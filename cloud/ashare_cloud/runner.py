"""Persistent Mac/Linux runner. Local state is authoritative; R2 is optional."""
from __future__ import annotations
import argparse
import os
import sys
import uuid
from pathlib import Path
from . import cli
from .common import now, read_json, write_json
from .local_state import Backup, restore, state_lock, validate_state


def run_once(work_dir, command='run', day=None, skip_ai=False, main_fn=None, cloud=False):
    os.umask(0o077)
    work_dir = Path(work_dir).resolve()
    with state_lock(work_dir) as root:
        validate_state(root)
        run_dir = work_dir / 'runs' / (now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10])
        meta = {'started_at': now().isoformat(), 'command': command, 'date': day,
                'status': 'running', 'state_dir': str(root), 'database_retained_locally': True,
                'cloud': 'pending' if cloud else 'disabled'}
        write_json(run_dir / 'run.json', meta)
        backup = None
        cloud_errors = []
        if cloud:
            try:
                backup = Backup()
            except Exception:
                cloud_errors.append('backup_configuration')
        def checkpoint():
            if backup:
                try:
                    backup.checkpoint(root)
                except Exception:
                    cloud_errors.append('checkpoint_upload')
                    print('进度已保存在本地；R2进度备份暂时失败。', flush=True)
        try:
            if command in ('run', 'review', 'sync'):
                args = [command, '--local', '--state-dir', str(root)]
                if day:
                    args += ['--date', day]
                if skip_ai:
                    args.append('--skip-ai')
                if main_fn:
                    main_fn(args)
                else:
                    cli.main(args, checkpoint=checkpoint)
            elif command == 'auction':
                from .auction import main
                if main(['--state-dir', str(root)]) != 0:
                    raise RuntimeError('竞价采集不完整；证据已保存在本地')
            elif command == 'backtest':
                from .backtest import replay
                result = replay(root)
                if result['status'] != 'ok':
                    raise RuntimeError(result['reason'])
            elif command not in ('publish', 'backup'):
                raise ValueError('不支持的任务')
            meta['status'] = 'ok'
        except BaseException:
            meta['status'] = 'failed'
            raise
        finally:
            # Always preserve progress, including partial meetings and failed fetches.
            write_json(root / "runtime-status.json", {**{k: meta[k] for k in ("command", "status", "started_at")}, "finished_at": now().isoformat()})
            if cloud:
                if backup:
                    try:
                        meta['backup'] = backup.full(root)
                    except Exception:
                        cloud_errors.append('full_backup')
                try:
                    from .publication import publish
                    meta['publication'] = publish(root)
                except Exception:
                    cloud_errors.append('publication')
                meta['cloud'] = 'failed' if cloud_errors else 'ok'
            meta['cloud_errors'] = sorted(set(cloud_errors))
            meta['finished_at'] = now().isoformat()
            write_json(run_dir / 'run.json', meta)
            write_json(work_dir / 'last-run.json', meta)
        if cloud_errors:
            raise RuntimeError('本地结果已保存；云端备份或发布失败，请运行 publish 重试，详见 last-run.json')
        return run_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地持久化运行；不会每次从R2恢复或删除数据库')
    parser.add_argument('--work-dir', type=Path, default=Path('.local-runtime'))
    parser.add_argument('--command', choices=('run','review','sync','auction','backtest','publish','backup','restore','import'), default='run')
    parser.add_argument('--date')
    parser.add_argument('--skip-ai', action='store_true')
    parser.add_argument('--cloud', action='store_true', help='备份与网页结果上传R2；不改变本地权威状态')
    parser.add_argument('--source', type=Path, help='import：已有本地账户目录')
    parser.add_argument('--from-backup', action='store_true', help='restore：从新架构backups恢复；默认承接旧R2')
    parser.add_argument('--version', help='restore：versions/<32位编号>.json，必须与--from-backup使用')
    args = parser.parse_args(argv)
    try:
        if args.command in ('restore', 'import'):
            if args.command == 'import' and not args.source:
                raise ValueError('import需要--source')
            if args.version and not args.from_backup:
                raise ValueError('--version需要--from-backup')
            store = Backup().store if args.from_backup else None
            restore(args.work_dir, store, args.source if args.command == 'import' else None, args.version)
            print('本地状态已恢复并校验；原始资料未修改。')
        else:
            path = run_once(args.work_dir, args.command, args.date, args.skip_ai, cloud=args.cloud)
            print('任务完成：' + str(path))
    except Exception as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__ + '；检查本地状态、网络和配置。'
        print('ERROR: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
