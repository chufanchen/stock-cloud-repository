import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from ashare_cloud import cli, meeting, review
from ashare_cloud.common import SHANGHAI, file_hash, read_json, write_json
from ashare_cloud.storage import Store
from test_engine import cal, cfg, market, pick, state
from test_storage import FakeS3


def role_post():
    return {"summary": "测试观点；没有执行真实回测。", "observations": ["事实由程序提供。"],
            "questions": ["需要更多样本。"], "memory_notes": ["不要把假设当结论。"]}


def summary_post(related=""):
    return {"summary": "测试会议汇总", "agreements": ["数据不足"], "disagreements": ["假设未验证"],
            "shared_memory": ["先登记假设，再验证；本次未验证。"],
            "proposals": [{"related_proposal_id": related, "title": "拒单与可成交性研究", "hypothesis": "拒单样本可能存在选择偏差。",
                           "evidence": "仅为测试提案，不是真实结果。", "test_plan": "按时间分训练与测试，核对费用和可成交性。",
                           "rejection_criterion": "样本外方向反转即否定。", "data_needed": "更完整的竞价数据。"}]}


def fake_model(calls, fail_at=None, related=""):
    def call(system, data, **kwargs):
        calls.append(copy.deepcopy(data))
        if fail_at and len(calls) == fail_at:
            raise RuntimeError("private provider body must not be logged")
        payload = summary_post(related) if "你是会议主持人" in system else role_post()
        return {"status": "ok", "content": json.dumps(payload, ensure_ascii=False), "model": "fixture-model",
                "generated_at": "2026-10-19T20:37:00+08:00", "usage": {"total_tokens": 30}}
    return call


def test_independent_analysis_audit_summary_and_idempotence(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(meeting, "chat_completion", fake_model(calls))
    write_json(tmp_path / "account.json", {"frozen": "do not change"})
    account_hash = file_hash(tmp_path / "account.json")
    facts = {"date": "2026-10-19", "equity": 100000}
    result = meeting.run_meeting(tmp_path, facts)
    assert len(calls) == 6
    assert all("current_posts" not in x for x in calls[:4])
    assert len(calls[4]["current_posts"]) == 4 and len(calls[5]["current_posts"]) == 5
    assert result["status"] == "ok" and result["kind"] == "meeting"
    memory = read_json(tmp_path / "meeting/memory/2026-10-19.json")
    assert len(memory["roles"]) == 5 and memory["shared"]
    proposals = read_json(tmp_path / "research/proposals.json")
    assert proposals["auto_apply"] is False
    assert proposals["proposals"][0]["status"] == "pending_validation"
    monkeypatch.setattr(meeting, "chat_completion", lambda *a, **kw: pytest.fail("completed meeting must be reused"))
    assert meeting.run_meeting(tmp_path, facts) == result
    assert file_hash(tmp_path / "account.json") == account_hash
    assert read_json(tmp_path / "research/proposals.json") == proposals


def test_failure_checkpoints_to_r2_and_resumes_only_incomplete_stages(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "market.db").write_bytes(b"test database")
    write_json(root / "account.json", {"last_settled": "2026-10-19"})
    storage = Store(FakeS3(), "bucket", "meeting")
    storage.push(root, bootstrap=True)
    calls = []
    monkeypatch.setattr(meeting, "chat_completion", fake_model(calls, fail_at=3))
    facts = {"date": "2026-10-19"}
    with pytest.raises(meeting.MeetingError, match="kostolany"):
        meeting.run_meeting(root, facts, checkpoint=lambda: storage.push(root))
    saved = read_json(root / "meeting/days/2026-10-19.json")
    assert len(saved["stages"]) == 3 and saved["stages"]["buffett"]["status"] == "ok"
    assert "private provider body" not in json.dumps(saved)
    restored = tmp_path / "restored"
    storage.pull(restored)
    calls.clear()
    monkeypatch.setattr(meeting, "chat_completion", fake_model(calls))
    meeting.run_meeting(restored, facts, checkpoint=lambda: storage.push(restored))
    assert len(calls) == 4
    restored2 = tmp_path / "restored2"
    storage.pull(restored2)
    assert read_json(restored2 / "meeting/memory/2026-10-19.json")["shared"]
    assert read_json(restored2 / "research/proposals.json")["proposals"]


def test_cross_day_memory_proposal_followup_and_no_future_context(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(meeting, "chat_completion", fake_model(calls))
    meeting.run_meeting(tmp_path, {"date": "2026-10-19"})
    pid = read_json(tmp_path / "research/proposals.json")["proposals"][0]["id"]
    calls.clear()
    monkeypatch.setattr(meeting, "chat_completion", fake_model(calls, related=pid))
    meeting.run_meeting(tmp_path, {"date": "2026-10-20"})
    assert calls[0]["previous_memory"]["date"] == "2026-10-19"
    assert calls[0]["previous_memory"]["roles"]["livermore"] == role_post()["memory_notes"]
    assert calls[0]["known_proposals"][0]["id"] == pid
    proposals = read_json(tmp_path / "research/proposals.json")["proposals"]
    assert len(proposals) == 1 and len(proposals[0]["observations"]) == 2
    previous, known = meeting.prior_context(tmp_path, "2026-10-20", 30)
    assert previous["date"] == "2026-10-19" and known[0]["updated_date"] == "2026-10-19"
    previous, known = meeting.prior_context(tmp_path, "2026-10-19", 30)
    assert previous["date"] is None and known == []
    future_memory = file_hash(tmp_path / "meeting/memory/2026-10-20.json")
    meeting.run_meeting(tmp_path, {"date": "2026-10-19"})
    assert len(read_json(tmp_path / "research/proposals.json")["proposals"][0]["observations"]) == 2
    assert file_hash(tmp_path / "meeting/memory/2026-10-20.json") == future_memory


def test_no_publishing_invalid_json_or_executable_rule_changes(tmp_path, monkeypatch):
    def invalid(system, data, **kw):
        response = role_post()
        response["apply_rules"] = {"position_threshold": 1}
        return {"content": json.dumps(response)}
    monkeypatch.setattr(meeting, "chat_completion", invalid)
    with pytest.raises(meeting.MeetingError):
        meeting.run_meeting(tmp_path, {"date": "2026-10-19"})
    assert not (tmp_path / "meeting/memory/2026-10-19.json").exists()
    assert not (tmp_path / "research/proposals.json").exists()
    assert "apply_rules" not in (tmp_path / "meeting/days/2026-10-19.json").read_text()


@pytest.mark.parametrize("change", ["related", "status", "oversize", "truncated"])
def test_strict_proposal_schema(change):
    payload = summary_post()
    if change == "related":
        payload["proposals"][0]["related_proposal_id"] = "nonexistent"
    elif change == "status":
        payload["proposals"][0]["status"] = "approved"
    elif change == "oversize":
        payload["shared_memory"] = ["x" * 301]
    text = json.dumps(payload)
    if change == "truncated":
        text = text[:-20]
    with pytest.raises(ValueError):
        meeting.parse_response(text, "moderator", set())


def test_changed_facts_refused_and_storage_errors_not_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting, "chat_completion", fake_model([]))
    count = 0
    def checkpoint():
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("R2 conflict")
    with pytest.raises(RuntimeError, match="R2 conflict") as e:
        meeting.run_meeting(tmp_path, {"date": "2026-10-19"}, checkpoint=checkpoint)
    assert not isinstance(e.value, meeting.MeetingError)
    saved = read_json(tmp_path / "meeting/days/2026-10-19.json")
    assert saved["stages"]["livermore"]["status"] == "ok" and saved["status"] == "running"
    with pytest.raises(meeting.MeetingError, match="事实或协议"):
        meeting.run_meeting(tmp_path, {"date": "2026-10-19", "new": "facts"})


def test_pipeline_meeting_failure_preserves_account_and_review_only_recovers(tmp_path, monkeypatch, cfg, cal, state):
    write_json(tmp_path / "account.json", state)
    m = market("2026-10-19")
    write_json(tmp_path / "snapshots/2026-10-19.json", m)
    monkeypatch.setattr(cli, "collect", lambda *a: m)
    monkeypatch.setattr(cli, "scan_database", lambda *a: {"hits": [pick()], "pool": [pick()]})
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 10, 19, 20, 37, tzinfo=SHANGHAI))
    monkeypatch.setattr(meeting, "chat_completion", fake_model([], fail_at=5))
    with pytest.raises(RuntimeError, match="账本和失败状态已保存"):
        cli.execute(tmp_path, "2026-10-19", cfg, cal)
    account_hash = file_hash(tmp_path / "account.json")
    frozen = read_json(tmp_path / "account.json")["plans"]["2026-10-20"]
    assert frozen["config"] == cfg  # no meeting settings enter the frozen strategy
    monkeypatch.setattr(cli, "collect", lambda *a: pytest.fail("review-only must not collect or settle"))
    calls = []
    monkeypatch.setattr(meeting, "chat_completion", fake_model(calls))
    cli.execute(tmp_path, "2026-10-19", cfg, cal, review_only=True)
    assert len(calls) == 2 and file_hash(tmp_path / "account.json") == account_hash
    cli.execute(tmp_path, "2026-10-19", cfg, cal, review_only=True)
    assert len(calls) == 2
    assert "AI会议室" in (tmp_path / "meeting/days/2026-10-19.md").read_text()


def test_review_only_missing_settlement_refused(tmp_path, cfg, cal, state):
    write_json(tmp_path / "account.json", state)
    with pytest.raises(ValueError, match="尚未结算"):
        cli.execute(tmp_path, "2026-10-19", cfg, cal, review_only=True)


def test_historical_review_evidence_excludes_future_results(cfg, cal, state):
    from ashare_cloud.engine import simulate
    s, _ = simulate(state, None, market(), "2026-10-20", cal)
    first = review.evidence(s, "2026-10-20", market())
    s["settlements"]["2026-10-21"] = {"equity": 99999, "day_pnl": -1}
    s["closed_trades"].append({"sell_date": "2026-10-21"})
    assert review.evidence(s, "2026-10-20", market()) == first


def test_model_transport_has_limits_and_no_tools(monkeypatch):
    monkeypatch.setenv("AI_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("AI_MODEL", "base-model")
    monkeypatch.setenv("AI_API_KEY", "fixture-key")
    class Response:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    def post(url, **kw):
        body = kw["json"]
        assert body["model"] == "override" and body["max_tokens"] == 1800
        assert "tools" not in body and "fixture-key" not in json.dumps(body)
        return Response()
    monkeypatch.setattr(review.requests, "post", post)
    review.chat_completion("test", {}, model="override", max_tokens=1800)
