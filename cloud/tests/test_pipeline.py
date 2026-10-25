import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from ashare_cloud import cli, review
from ashare_cloud.common import SHANGHAI, read_json, write_json
from test_engine import cal, cfg, market, pick, state


def test_ai_failure_keeps_financial_state_and_retry_only_reviews(tmp_path, monkeypatch, cfg, cal, state):
    root = tmp_path
    write_json(root / "account.json", state)
    m = market("2026-10-19")
    write_json(root / "snapshots/2026-10-19.json", m)
    monkeypatch.setattr(cli, "collect", lambda *a: m)
    monkeypatch.setattr(cli, "scan_database", lambda *a: {"hits": [pick()], "pool": [pick()]})
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 10, 19, 20, 37, tzinfo=SHANGHAI))
    monkeypatch.setattr(cli, "ai_review", lambda *a: (_ for _ in ()).throw(RuntimeError("mock AI failure")))
    with pytest.raises(RuntimeError, match="账本和失败状态已保存"):
        cli.execute(root, "2026-10-19", cfg, cal, meeting_cfg={"enabled": False})
    saved = read_json(root / "account.json")
    assert saved["last_settled"] == "2026-10-19" and "2026-10-20" in saved["plans"]
    assert read_json(root / "reviews/2026-10-19.json")["status"] == "failed"
    monkeypatch.setattr(cli, "collect", lambda *a: pytest.fail("must not refetch on review retry"))
    monkeypatch.setattr(cli, "ai_review", lambda *a: {"status": "ok", "content": "测试用AI响应，不是真实模型调用。"})
    cli.execute(root, "2026-10-19", cfg, cal, meeting_cfg={"enabled": False})
    assert read_json(root / "account.json") == saved
    assert read_json(root / "reviews/2026-10-19.json")["status"] == "ok"
    monkeypatch.setattr(cli, "ai_review", lambda *a: pytest.fail("must not call AI twice"))
    cli.execute(root, "2026-10-19", cfg, cal, meeting_cfg={"enabled": False})


def test_ai_request_no_secrets_in_evidence_and_no_redirects(monkeypatch):
    monkeypatch.setenv("AI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("AI_MODEL", "model-test")
    monkeypatch.setenv("AI_API_KEY", "test-secret-value")
    class Response:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "测试"}, "finish_reason": "stop"}]}
    def post(url, **kw):
        assert url == "https://model.example/v1/chat/completions"
        assert kw["allow_redirects"] is False
        assert "test-secret-value" not in json.dumps(kw["json"])
        return Response()
    monkeypatch.setattr(review.requests, "post", post)
    assert review.ai_review({"date": "2026-10-19"})["status"] == "ok"


def test_html_escapes_model_output(tmp_path, cfg, cal, state):
    from ashare_cloud.engine import simulate
    s, _ = simulate(state, None, market(), "2026-10-20", cal)
    facts = review.evidence(s, "2026-10-20", market())
    review.render(tmp_path, facts, {"status": "ok", "content": "<script>alert(1)</script>"})
    text = (tmp_path / "reports/2026-10-20.html").read_text()
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_workflow_contains_state_serialization_and_no_write_permissions():
    import yaml
    root = Path(__file__).parents[2]
    flow = yaml.safe_load((root / ".github/workflows/daily.yml").read_text())
    assert flow["concurrency"]["cancel-in-progress"] is False
    assert flow["permissions"] == {"contents": "read"}
    assert flow["jobs"]["daily"]["env"]["TZ"] == "Asia/Shanghai"


def test_three_day_pipeline_with_plan_chain(tmp_path, monkeypatch, cfg, cal, state):
    write_json(tmp_path / "account.json", state)
    monkeypatch.setattr(cli, "scan_database", lambda *a: {"hits": [pick()], "pool": [pick()]})
    for day, opening, closing, prev in [("2026-10-19", 10, 10, 9.09),
                                       ("2026-10-20", 10.2, 10.3, 10),
                                       ("2026-10-21", 10.3, 10.4, 10.3)]:
        m = market(day, opening, closing, prev)
        write_json(tmp_path / f"snapshots/{day}.json", m)
        monkeypatch.setattr(cli, "collect", lambda *a, captured=m: captured)
        monkeypatch.setattr(cli, "now", lambda d=day: datetime.fromisoformat(d + "T20:37:00+08:00"))
        cli.execute(tmp_path, day, cfg, cal, skip_ai=True)
    s = read_json(tmp_path / "account.json")
    cli.check_account(s)
    assert len(s["settlements"]) == 3 and len(s["closed_trades"]) == 1
    assert not s["positions"] and s["cash"] > state["cash"]
    assert set(s["plans"]) == {"2026-10-20", "2026-10-21", "2026-10-22"}
