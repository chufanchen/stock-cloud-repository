import fcntl
from pathlib import Path

import pytest

from ashare_cloud.common import read_json
from ashare_cloud.runner import run_once


def test_server_fresh_state_per_attempt_and_lock(tmp_path):
    seen = []
    def fake_main(args):
        path = Path(args[args.index("--state-dir") + 1])
        assert not path.exists() and "--local" not in args
        assert args[0] == "review" and args[-1] == "2026-10-19"
        path.mkdir()
        (path / "market.db").write_bytes(b"temporary restored copy")
        seen.append(path)
        with pytest.raises(RuntimeError, match="已有任务"):
            run_once(tmp_path, main_fn=lambda a: pytest.fail("overlapping run"))
    first = run_once(tmp_path, "review", "2026-10-19", main_fn=fake_main)
    second = run_once(tmp_path, "review", "2026-10-19", main_fn=fake_main)
    assert seen[0] != seen[1]
    assert all(not (p / "market.db").exists() for p in seen)
    assert read_json(first / "run.json")["status"] == read_json(second / "run.json")["status"] == "ok"


def test_server_failure_preserved_and_next_attempt_unblocked(tmp_path):
    def fail(args):
        raise RuntimeError("mock failure")
    with pytest.raises(RuntimeError):
        run_once(tmp_path, main_fn=fail)
    failed = list((tmp_path / "runs").glob("*/run.json"))
    assert len(failed) == 1 and read_json(failed[0])["status"] == "failed"
    result = run_once(tmp_path, main_fn=lambda args: None)
    assert read_json(result / "run.json")["status"] == "ok"
