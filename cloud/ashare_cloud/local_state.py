"""Persistent single-writer state, consistent SQLite backups, explicit restoration."""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path
from .common import file_hash, now, read_json, write_json
from .storage import Store


@contextlib.contextmanager
def state_lock(work_dir):
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (work_dir / 'runner.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('本机已有任务运行，本次不启动重复任务。') from None
        yield work_dir / 'state'


def validate_state(root):
    from .cli import check_account
    check_account(read_json(Path(root) / 'account.json'))
    with sqlite3.connect((Path(root) / 'market.db').resolve().as_uri() + '?mode=ro', uri=True) as con:
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('数据库完整性校验失败')


def copy_state(source, target, database=True):
    """Caller holds state lock; SQLite backup includes committed WAL pages."""
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError('状态目录不允许符号链接')
        if path.is_file() and path.suffix in ('.json', '.md', '.html'):
            out = target / path.relative_to(source)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
    if database:
        with sqlite3.connect((source / 'market.db').resolve().as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(target / 'market.db') as dst:
                src.backup(dst)


def restore(work_dir, store=None, source=None, version=None):
    """Only installs into a nonexistent state directory; never merges or replaces."""
    with state_lock(work_dir) as root:
        if root.exists():
            raise ValueError('本地状态目录已存在；请用新的工作目录恢复并核对，不会覆盖')
        with tempfile.TemporaryDirectory(dir=Path(work_dir)) as tmp:
            stage = Path(tmp) / 'state'
            if source:
                if version:
                    raise ValueError('本地导入不能同时指定远端版本')
                copy_state(source, stage)
            else:
                store = store or Store()
                if version:
                    # The version is a relative immutable manifest, not an arbitrary object key.
                    import re
                    if not re.fullmatch(r'versions/[a-f0-9]{32}\.json', version):
                        raise ValueError('备份版本格式错误')
                    manifest = json.loads(store.client.get_object(Bucket=store.bucket, Key=store.key(version))['Body'].read())
                    store.pull(stage, manifest=manifest)
                else:
                    store.pull(stage)
            validate_state(stage)
            os.replace(stage, root)
        return root


class Backup:
    def __init__(self, store=None):
        base = store or Store()
        self.store = Store(base.client, base.bucket, base.prefix + '/backups')

    def checkpoint(self, root):
        """Small private progress archive; not a complete database restore point."""
        store = self.store
        with tempfile.TemporaryDirectory() as tmp:
            stage, archive = Path(tmp) / 'state', Path(tmp) / 'checkpoint.tar.gz'
            copy_state(root, stage, database=False)
            with tarfile.open(archive, 'w:gz') as tar:
                for path in sorted(stage.rglob('*')):
                    if path.is_file():
                        tar.add(path, arcname=path.relative_to(stage), recursive=False)
            key = store.key('checkpoints/' + uuid.uuid4().hex + '.tar.gz')
            store.client.upload_file(str(archive), store.bucket, key)
        return key

    def full(self, root):
        store = self.store
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / 'state'
            copy_state(root, stage)
            validate_state(stage)
            exists = store.head() is not None
            manifest = store.push(stage, bootstrap=not exists)
            version = 'versions/' + uuid.uuid4().hex + '.json'
            store.client.put_object(Bucket=store.bucket, Key=store.key(version),
                Body=json.dumps(manifest).encode(), ContentType='application/json', IfNoneMatch='*')
        return {'version': version, 'completed_at': now().isoformat(), 'last_settled': manifest['last_settled']}


def start_observation(work_dir, source, start, calendar=None):
    """Explicit new epoch; transfer verified flat cash, never invent missing sessions."""
    from .common import Calendar
    calendar = calendar or Calendar()
    source = Path(source).resolve()
    validate_state(source)
    old = read_json(source / 'account.json')
    if old['positions'] or abs(old['cash'] - old['equity']) > 0.011:
        raise ValueError('仅允许已验证空仓账户开启新观察期；持仓需要单独迁移设计')
    today = now().date().isoformat()
    if start <= today or not calendar.is_session(start):
        raise ValueError('新观察期必须是未来交易日')
    with state_lock(work_dir) as root:
        if root.exists():
            raise ValueError('目标已存在；不覆盖现有观察期')
        with tempfile.TemporaryDirectory(dir=Path(work_dir)) as tmp:
            stage = Path(tmp) / 'state'; stage.mkdir()
            with sqlite3.connect((source/'market.db').as_uri()+'?mode=ro',uri=True) as src:
                with sqlite3.connect(stage/'market.db') as dst:
                    src.backup(dst)
            account = {'schema':1, 'created_at':now().isoformat(), 'cash':old['cash'], 'equity':old['cash'],
                'positions':[], 'closed_trades':[], 'last_settled':None, 'settlements':{}, 'plans':{}, 'last_plan_hash':None,
                'provenance':{'original_seed_cash':old['cash'], 'observation_start':start,
                    'predecessor_account_sha256':file_hash(source/'account.json'), 'predecessor_last_settled':old['last_settled'],
                    'note':'用户确认的新观察期，承接空仓现金；历史缺口不生成成交或收益。'}}
            write_json(stage/'account.json',account)
            validate_state(stage)
            os.replace(stage,root)
        return root
