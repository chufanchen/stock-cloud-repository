"""Linux/macOS scheduled entry point: one local process, fresh R2 checkout per attempt."""
from __future__ import annotations

import argparse
import fcntl
import sys
import uuid
from pathlib import Path

from . import cli
from .common import now, write_json


def run_once(work_dir, command="run", day=None, skip_ai=False, main_fn=None):
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (work_dir / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("服务器已有任务运行，本次不启动重复任务。") from None
        run_dir = work_dir / "runs" / (now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10])
        run_dir.mkdir(parents=True, mode=0o700)
        meta = {"started_at": now().isoformat(), "command": command, "date": day,
                "status": "running", "state_dir": "state"}
        write_json(run_dir / "run.json", meta)
        args = [command, "--state-dir", str(run_dir / "state")]
        if day:
            args += ["--date", day]
        if skip_ai:
            args.append("--skip-ai")
        print(f"本次运行目录：{run_dir}", flush=True)
        try:
            (main_fn or cli.main)(args)
            meta["status"] = "ok"
            # This is a newly generated R2 checkout, never the user's source database.
            # R2 retains the committed database; keep reports locally without daily 120 MB copies.
            (run_dir / "state/market.db").unlink(missing_ok=True)
            meta["database_retained_locally"] = False
        except BaseException:
            meta["status"] = "failed"
            raise
        finally:
            meta["finished_at"] = now().isoformat()
            write_json(run_dir / "run.json", meta)
        return run_dir


def main():
    parser = argparse.ArgumentParser(description="服务器定时入口；每次从R2恢复，保留运行目录便于排查")
    parser.add_argument("--work-dir", type=Path, default=Path(".server-runs"))
    parser.add_argument("--command", choices=("run", "review"), default="run")
    parser.add_argument("--date")
    parser.add_argument("--skip-ai", action="store_true")
    args = parser.parse_args()
    try:
        run_once(args.work_dir, args.command, args.date, args.skip_ai)
    except Exception as exc:
        from botocore.exceptions import BotoCoreError, ClientError
        if isinstance(exc, (BotoCoreError, ClientError)):
            message = "R2请求失败，请检查端点、权限和网络。"
        elif isinstance(exc, (ValueError, RuntimeError)):
            message = str(exc)
        else:
            message = type(exc).__name__ + "；请检查配置和输入文件。"
        print("ERROR: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
