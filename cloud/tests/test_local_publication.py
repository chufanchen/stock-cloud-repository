from datetime import datetime
import json
import pytest
from ashare_cloud.common import SHANGHAI, file_hash, read_json, write_json
from ashare_cloud import publication, pages, cli
from ashare_cloud.backtest import replay
from ashare_cloud.desktop import schedule_slot, load_env
from ashare_cloud.storage import Store
from test_storage import FakeS3
from test_pages import seed
from test_engine import state, cfg, cal, market, pick


def test_public_bundle_has_no_private_state_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.delenv('R2_PUBLIC_BUCKET', raising=False)
    source = tmp_path/'state'; seed(source)
    write_json(source/'account.json', {'plans': {}, 'SECRET': 'PRIVATE_ACCOUNT'})
    write_json(source/'runtime-status.json', {'command':'review','status':'failed','started_at':'2026-10-30T20:37:00+08:00','secret':'PRIVATE_RUNTIME'})
    client = FakeS3(); store = Store(client, 'bucket','prefix')
    publication.publish(source,store)
    assert all('/publications/' in k for k in client.files)
    assert b'PRIVATE_' not in b''.join(client.files.values())
    bundle = publication.fetch(store)
    pages.build_site(tmp_path/'none',tmp_path/'site',public_data=bundle['data'])
    assert '2026-10-30T20:37:00+08:00' in (tmp_path/'site/index.html').read_text()
    assert (tmp_path/'site/plans.html').exists() and (tmp_path/'site/backtests.html').exists()
    key = json.loads(client.files['prefix/publications/HEAD.json'])['key']
    client.files[key] += b'corrupt'
    with pytest.raises(ValueError, match='校验'):
        publication.fetch(store)


def test_replay_and_plan_export_dont_change_account(tmp_path, monkeypatch, cfg, cal, state):
    state['provenance'] = {'original_seed_cash': state['cash']}
    write_json(tmp_path/'account.json',state)
    monkeypatch.setattr(cli, 'scan_database', lambda *a:{'hits':[pick()],'pool':[pick()]})
    for day, opening, closing, prev in [('2026-10-19',10,10,9.09),('2026-10-20',10.2,10.3,10),('2026-10-21',10.3,10.4,10.3)]:
        data = market(day,opening,closing,prev)
        write_json(tmp_path/f'snapshots/{day}.json',data)
        monkeypatch.setattr(cli,'collect',lambda *a, captured=data: captured)
        monkeypatch.setattr(cli,'now',lambda d=day:datetime.fromisoformat(d+'T20:37:00+08:00'))
        cli.execute(tmp_path,day,cfg,cal,skip_ai=True)
    before = file_hash(tmp_path/'account.json')
    result = replay(tmp_path,cal)
    assert result['status']=='ok' and result['sessions']==3 and result['sells']==1
    assert file_hash(tmp_path/'account.json')==before
    data = pages.load_public_data(tmp_path)
    assert len(data['plans'])==3 and data['backtests'][0]['status']=='ok'
    assert 'config' not in json.dumps(data['plans'])
    (tmp_path/'snapshots/2026-10-20.json').unlink()
    assert replay(tmp_path,cal)['status']=='blocked'
    assert file_hash(tmp_path/'account.json')==before


@pytest.mark.parametrize('clock,expected',[('09:24:49',None),('09:25:05',('auction','auction')),('09:30:00',None),('20:38:00',('20','run')),('22:20:00',('backtest','backtest')),('23:00:00',None)])
def test_shanghai_scheduler_windows(clock, expected):
    assert schedule_slot(datetime.fromisoformat('2026-10-30T'+clock+'+08:00')) == expected


def test_env_is_data_not_shell(tmp_path, monkeypatch):
    p=tmp_path/'env';p.write_text('AI_MODEL="$(touch /tmp/never-execute)"\nHOME=/not-your-home\n')
    monkeypatch.delenv('AI_MODEL',raising=False)
    import os
    home=os.environ.get('HOME');load_env(p)
    assert os.environ['AI_MODEL']=='$(touch /tmp/never-execute)' and os.environ.get('HOME')==home


def test_new_observation_prepares_without_early_settlement(tmp_path, monkeypatch, cfg, cal, state):
    state['provenance']={'observation_start':'2026-11-02','original_seed_cash':state['cash']}
    write_json(tmp_path/'account.json',state)
    monkeypatch.setattr(cli,'now',lambda:datetime.fromisoformat('2026-10-30T20:37:00+08:00'))
    monkeypatch.setattr(cli,'collect',lambda *a,**k:None)
    monkeypatch.setattr(cli,'scan_database',lambda *a:{'hits':[pick()],'pool':[pick()]})
    cli.execute(tmp_path,'2026-10-30',cfg,cal)
    actual=read_json(tmp_path/'account.json')
    assert actual['last_settled'] is None and actual['settlements']=={} and '2026-11-02' in actual['plans']
    before=file_hash(tmp_path/'account.json')
    cli.execute(tmp_path,'2026-10-30',cfg,cal)
    assert file_hash(tmp_path/'account.json')==before
    pages.build_site(tmp_path,tmp_path.parent/(tmp_path.name+'-site'))
    assert '2026-11-02' in (tmp_path.parent/(tmp_path.name+'-site')/'plans.html').read_text()
