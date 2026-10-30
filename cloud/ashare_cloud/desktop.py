"""Mac launcher: read env without shell execution; schedule by Shanghai wall time."""
from __future__ import annotations
import argparse
import os
import plistlib
import shlex
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from .common import now, read_json, write_json


def load_env(path):
    for token in shlex.split(Path(path).read_text(), comments=True):
        if '=' in token:
            key, value = token.split('=', 1)
            if key.startswith(('R2_', 'AI_', 'TICKFLOW_', 'TUSHARE_')):
                os.environ[key] = value


def schedule_slot(current):
    if current.weekday() > 4:
        return None
    clock = current.strftime('%H%M%S')
    if '092450' <= clock <= '092550':
        return 'auction', 'auction'
    for hour in (20, 21):
        if f'{hour}3700' <= clock < f'{hour}4200':
            return str(hour), 'run'
    if '222000' <= clock < '222500':
        return 'backtest', 'backtest'
    return None


def pulse(project, work_dir):
    from .runner import run_once
    from .local_state import state_lock
    from .common import Calendar
    current = now()
    slot = schedule_slot(current)
    if slot is None or not Calendar().is_session(current.date().isoformat()):
        return
    key = current.date().isoformat() + '-' + slot[0]
    path = work_dir / 'schedule.json'
    # Reserve once under the same lock. Sleeping outside a window never backfills it.
    with state_lock(work_dir):
        slots = read_json(path) if path.exists() else {}
        if key in slots:
            return
        slots = {k: v for k, v in slots.items() if k[:10] >= (current.date()-timedelta(days=14)).isoformat()}
        slots[key] = {'started_at': current.isoformat(), 'status': 'running'}
        write_json(path, slots)
    try:
        run_once(work_dir, slot[1], cloud=True)
        status = 'ok'
    except Exception as exc:
        status = 'failed'
        print('定时任务失败：' + (str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__), flush=True)
    with state_lock(work_dir):
        slots = read_json(path)
        slots[key]['status'] = status
        write_json(path, slots)


def install(project, work_dir):
    if sys.platform != 'darwin':
        raise ValueError('此安装器仅用于macOS；Linux使用systemd模板')
    project, work_dir = project.resolve(), work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (work_dir / 'logs').mkdir(exist_ok=True)
    label = 'org.wuming.local-daily'
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError('定时配置已存在，请先检查现有配置；不会覆盖')
    content = {'Label': label, 'ProgramArguments': [str(project / '.venv/bin/python'), '-m', 'ashare_cloud.desktop', 'pulse',
        '--project', str(project), '--work-dir', str(work_dir)],
        'WorkingDirectory': str(project), 'EnvironmentVariables': {'PYTHONPATH': str(project/'cloud'), 'PYTHONUNBUFFERED':'1'},
        'StartInterval': 15, 'RunAtLoad': True, 'ProcessType': 'Background',
        'StandardOutPath': str(work_dir/'logs/schedule.log'), 'StandardErrorPath': str(work_dir/'logs/schedule-error.log')}
    with path.open('wb') as out:
        plistlib.dump(content, out)
    os.chmod(path, 0o600)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    print('已安装Mac定时任务：' + str(path))


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('pulse','install','task'))
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--command', default='publish')
    parser.add_argument('--date')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--from-backup', action='store_true')
    parser.add_argument('--version')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    active = args.project / '.local-active.json'
    default_work = Path(read_json(active)['work_dir']) if active.exists() else args.project / '.local-runtime'
    work = args.work_dir or default_work
    if args.action == 'install':
        install(args.project, work)
        return 0
    load_env(args.project / '.cloud.env')
    if args.action == 'pulse':
        pulse(args.project, work)
        return 0
    from .runner import main as run
    command = ['--work-dir', str(work), '--command', args.command]
    for flag, val in (('--date',args.date),('--source',args.source),('--version',args.version)):
        if val:
            command += [flag,str(val)]
    if args.from_backup:
        command.append('--from-backup')
    if not args.offline:
        command.append('--cloud')
    return run(command)


if __name__ == '__main__':
    raise SystemExit(main())
