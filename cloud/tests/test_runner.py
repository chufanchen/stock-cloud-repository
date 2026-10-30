from pathlib import Path
import sqlite3
import pytest
from ashare_cloud.common import read_json, write_json, file_hash
from ashare_cloud.runner import run_once
from ashare_cloud.local_state import Backup, restore, copy_state
from ashare_cloud.storage import Store
from test_storage import FakeS3
from test_engine import state


def seed(root, state):
    root.mkdir(parents=True)
    write_json(root / 'account.json', state)
    with sqlite3.connect(root / 'market.db') as con:
        con.execute('create table quotes(x integer)')
        con.execute('insert into quotes values (7)')


def test_persistent_state_reused_and_lock(tmp_path, state):
    seed(tmp_path/'state', state)
    seen = []
    def main(args):
        path = Path(args[args.index('--state-dir') + 1])
        assert '--local' in args and path.exists()
        seen.append(path)
        with pytest.raises(RuntimeError, match='已有任务'):
            run_once(tmp_path, main_fn=lambda a: pytest.fail('overlap'))
    run_once(tmp_path, main_fn=main)
    run_once(tmp_path, main_fn=main)
    assert seen == [tmp_path/'state']*2
    assert (seen[0]/'market.db').exists()


def test_failure_keeps_progress_and_unlocks(tmp_path, state):
    seed(tmp_path/'state', state)
    def fail(args):
        write_json(tmp_path/'state/meeting/progress.json', {'completed': 4})
        raise RuntimeError('AI unavailable')
    with pytest.raises(RuntimeError):
        run_once(tmp_path, main_fn=fail)
    assert read_json(tmp_path/'last-run.json')['status'] == 'failed'
    assert read_json(tmp_path/'state/meeting/progress.json')['completed'] == 4
    run_once(tmp_path, main_fn=lambda a: None)
    assert read_json(tmp_path/'last-run.json')['status'] == 'ok'


def test_sqlite_wal_backup_and_explicit_restore(tmp_path, state):
    root = tmp_path/'source'; seed(root, state)
    con = sqlite3.connect(root/'market.db')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('insert into quotes values (8)'); con.commit()
    before = file_hash(root/'account.json')
    client = FakeS3(); backup = Backup(Store(client, 'bucket', 'prefix'))
    checkpoint = backup.checkpoint(root)
    result = backup.full(root)
    assert checkpoint in client.files and 'prefix/HEAD.json' not in client.files
    restored = restore(tmp_path/'new', backup.store, version=result['version'])
    with sqlite3.connect(restored/'market.db') as db:
        assert db.execute('select x from quotes').fetchall() == [(7,), (8,)]
    assert before == file_hash(restored/'account.json') == file_hash(root/'account.json')
    with pytest.raises(ValueError, match='已存在'):
        restore(tmp_path/'new', backup.store)
    con.close()


def test_failed_restore_leaves_no_active_state(tmp_path, state):
    root = tmp_path/'source'; seed(root, state)
    (root/'account.json').write_text('{}')
    with pytest.raises(ValueError):
        restore(tmp_path/'new', source=root)
    assert not (tmp_path/'new/state').exists()


def test_cloud_failure_does_not_erase_local_results(tmp_path, state, monkeypatch):
    from ashare_cloud import runner, publication
    seed(tmp_path/'state', state)
    class BadBackup:
        def full(self, root): raise OSError('network')
    monkeypatch.setattr(runner, 'Backup', BadBackup)
    monkeypatch.setattr(publication, 'publish', lambda root: {'ok': True})
    with pytest.raises(RuntimeError, match='本地结果已保存'):
        run_once(tmp_path, main_fn=lambda a: None, cloud=True)
    assert (tmp_path/'state/market.db').exists()
    status = read_json(tmp_path/'last-run.json')
    assert status['status']=='ok' and status['cloud']=='failed'


def test_explicit_new_epoch_preserves_old_and_needs_plan(tmp_path, state, monkeypatch):
    from ashare_cloud.local_state import start_observation
    from ashare_cloud import local_state, cli
    from ashare_cloud.common import Calendar
    from datetime import datetime
    source=tmp_path/'old';seed(source,state)
    before=file_hash(source/'account.json')
    monkeypatch.setattr(local_state,'now',lambda:datetime.fromisoformat('2026-10-30T20:00:00+08:00'))
    root=start_observation(tmp_path/'next',source,'2026-11-02')
    new=read_json(root/'account.json')
    assert new['cash']==state['cash'] and new['last_settled'] is None and new['plans']=={}
    assert file_hash(source/'account.json')==before
    with pytest.raises(ValueError,match='首日缺少'):
        cli.execute(root,'2026-11-02',{},Calendar())
