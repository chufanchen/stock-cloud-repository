import copy
import json
from datetime import datetime

import pytest
import requests

from ashare_cloud import cli, meeting, pages, research_data as rd, research_sources as rs, review
from ashare_cloud.common import SHANGHAI, digest, file_hash, read_json, write_json
from test_engine import cal, cfg, market, pick, state
from test_meeting import fake_model
from test_pages import seed

DAY = "2026-10-29"
STAMP = datetime(2026, 10, 29, 20, 37, tzinfo=SHANGHAI)


@pytest.fixture
def settings():
    return read_json(rd.DEFAULT_CONFIG)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.requests = 0
        self.blocked = {}

    def get(self, provider, url, params=None):
        self.calls.append((provider, url, params))
        self.requests += 1
        return self.response(provider, url, params) if callable(self.response) else copy.deepcopy(self.response)


def dc(rows, total=None, pages=1):
    return {"success": True, "result": {"data": rows, "pages": pages, "count": len(rows) if total is None else total}}


def lhb(symbol="600519"):
    return {"SECURITY_CODE": symbol, "SECURITY_NAME_ABBR": "测试", "TRADE_DATE": DAY + " 00:00:00",
            "EXPLANATION": "连续三个交易日", "TRADE_ID": "id1", "BILLBOARD_NET_AMT": 100,
            "D1_CLOSE_ADJCHRATE": 99, "D30_CLOSE_ADJCHRATE": 77, "BUY": 200, "SELL": 100, "NET": 100,
            "OPERATEDEPT_CODE": "seat1", "OPERATEDEPT_NAME": "测试席位", "RISE_PROBABILITY_3DAY": 0.99}


def test_lhb_whitelists_future_outcomes_and_preserves_reason():
    out = rs.fetch_dragon_tiger(FakeClient(dc([lhb()])), DAY, [], STAMP)
    assert out["complete"] and out["rows"][0]["reason"] == "连续三个交易日"
    assert "D1_CLOSE" not in json.dumps(out) and "D30_CLOSE" not in json.dumps(out)
    seats = rs.fetch_seats(FakeClient(dc([lhb()])), DAY, ["600519"], STAMP)
    assert len(seats["rows"]) == 2  # retain both sides; deliberately no aggregate NET
    assert {r["side"] for r in seats["rows"]} == {"buy", "sell"}
    assert "RISE_PROBABILITY" not in json.dumps(seats) and "net_yuan" not in seats


def test_lhb_rejects_wrong_date_duplicate_and_unstable_pages():
    bad = lhb(); bad["TRADE_DATE"] = "2026-10-28"
    with pytest.raises(rs.FeedError, match="wrong_source_date"):
        rs.fetch_dragon_tiger(FakeClient(dc([bad])), DAY, [], STAMP)
    with pytest.raises(rs.FeedError, match="duplicate"):
        rs.fetch_dragon_tiger(FakeClient(dc([lhb(), lhb()])), DAY, [], STAMP)
    client = FakeClient(lambda p, u, q: dc([lhb(str(600000 + q["pageNumber"]))], total=2 if q["pageNumber"] == 1 else 3, pages=2))
    with pytest.raises(rs.FeedError, match="unstable"):
        rs.fetch_dragon_tiger(client, DAY, [], STAMP)


def test_pagination_complete_and_partial():
    c = FakeClient(lambda p, u, q: dc([lhb(str(600000 + q["pageNumber"]))], total=2, pages=2))
    out = rs.fetch_dragon_tiger(c, DAY, [], STAMP)
    assert out["complete"] and out["row_count"] == 2 and len(c.calls) == 2
    rows, total, complete = rs.datacenter(c, "r", "f", "s", max_pages=1)
    assert not complete and total == 2 and len(rows) == 1


def test_pool_empty_is_distinct_from_unavailable():
    assert rs.fetch_pool(FakeClient({"rc": 0, "data": {"pool": [], "tc": 0}}), "limit_up", DAY)["row_count"] == 0
    with pytest.raises(rs.FeedError):
        rs.fetch_pool(FakeClient({"rc": 0, "data": None}), "limit_up", DAY)


def test_financials_disclosure_cutoff_field_whitelist_and_cumulative_labels():
    def response(p, u, q):
        return dc([
            {"SECURITY_CODE": "600519", "NOTICE_DATE": DAY, "REPORT_DATE": "2026-09-30", "TOTAL_ASSETS": 999},
            {"SECURITY_CODE": "600519", "NOTICE_DATE": "2026-08-15", "REPORT_DATE": "2026-06-30",
             "TOTAL_ASSETS": 100, "PARENT_NETPROFIT": 10, "NETCASH_OPERATE": 20, "UNTRUSTED_FUTURE": 1000}], 20, 10)
    out = rs.fetch_financials(FakeClient(response), DAY, ["600519"], STAMP)
    assert len(out["rows"]) == 3 and all(r["report_date"] == "2026-06-30" for r in out["rows"])
    assert all(r["published_at"] is None for r in out["rows"])
    assert "UNTRUSTED" not in json.dumps(out) and "不是单季度" in out["note"]
    assert not out["complete"]


def test_announcements_only_index_bounded_time_and_safe_url():
    row = {"codes": [{"stock_code": "600519"}], "display_time": "2026-10-28 20:41:29:380", "notice_date": "2026-10-29 00:00:00",
           "art_code": "AN202610281831115435", "title": "<script>instruction</script>公告", "columns": [], "raw_secret": "PRIVATE"}
    d = {"success": 1, "data": {"list": [row], "total_hits": 100}}
    out = rs.fetch_announcements(FakeClient(d), DAY, ["600519"], STAMP)
    assert out["row_count"] == 1 and not out["complete"] and "PRIVATE" not in json.dumps(out)
    assert out["rows"][0]["url"].startswith("https://data.eastmoney.com/notices/detail/600519/AN")
    row["display_time"] = "2026-10-30 01:00:00"
    out = rs.fetch_announcements(FakeClient(d), DAY, ["600519"], STAMP)
    assert out["row_count"] == 0 and out["errors"]


def test_news_and_sector_validate_dates():
    d = {"code": "1", "data": {"fastNewsList": [{"showTime": "2026-10-29 12:00:00", "title": "快讯"},
                    {"showTime": "2026-10-28 12:00:00", "title": "旧快讯"}]}}
    assert rs.fetch_news(FakeClient(d), DAY, [], STAMP)["row_count"] == 1
    d["data"]["fastNewsList"][0]["showTime"] = "2026-10-30 00:00:00"
    with pytest.raises(rs.FeedError, match="future"):
        rs.fetch_news(FakeClient(d), DAY, [], STAMP)
    row = {"f12": "BK1", "f14": "行业", "f124": datetime(2026, 10, 28, 15, 0, tzinfo=SHANGHAI).timestamp()}
    with pytest.raises(rs.FeedError, match="stale"):
        rs.fetch_sector(FakeClient({"rc": 0, "data": {"total": 1, "diff": [row]}}), DAY, [], STAMP)


class Response:
    def __init__(self, status=200, body=b'{}'):
        self.status_code, self.body = status, body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.body


class Session:
    def __init__(self, response): self.response, self.calls = response, []
    def get(self, *a, **kw):
        self.calls.append((a, kw))
        if isinstance(self.response, Exception): raise self.response
        return self.response


@pytest.mark.parametrize("status", [401, 403, 429])
def test_http_denial_stops_whole_provider_without_domain_bypass(status, settings, monkeypatch):
    monkeypatch.setattr(rs.time, "sleep", lambda _: None)
    session = Session(Response(status))
    client = rs.PublicClient(settings, session)
    for url in (rs.DC, "https://push2.eastmoney.com/api/qt/clist/get"):
        with pytest.raises(rs.FeedError, match=str(status)): client.get("eastmoney", url)
    assert len(session.calls) == 1 and client.blocked["eastmoney"] == f"http_{status}"
    assert session.calls[0][1]["allow_redirects"] is False


def test_transport_budgets_and_redacted_network_errors(settings, monkeypatch):
    monkeypatch.setattr(rs.time, "sleep", lambda _: None)
    c = rs.PublicClient(settings, Session(requests.ConnectionError("PRIVATE_TOKEN")))
    with pytest.raises(rs.FeedError, match="^network_error$"):
        c.get("eastmoney", rs.DC)
    assert c.requests == 2 and "PRIVATE" not in str(c.blocked)
    c = rs.PublicClient(settings, Session(Response(body=b'x' * (4 * 1024 * 1024 + 1))))
    with pytest.raises(rs.FeedError, match="response_limit"): c.get("eastmoney", rs.DC)
    c.requests = settings["max_requests"]
    with pytest.raises(rs.FeedError, match="budget_exhausted"): c.get("eastmoney", rs.DC)


def test_freeze_archive_reuse_checksum_and_no_history_backfill(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(rd, "now", lambda: STAMP)
    calls = []
    def fake_fetch(client, kind, day, symbols, captured):
        calls.append(kind)
        return rs.result([{"code": "600519", "name": "测试"}], 1)
    monkeypatch.setattr(rd, "fetch", fake_fetch)
    state = {"positions": [{"code": "600519"}]}
    doc = rd.collect_research(tmp_path, DAY, state, settings, FakeClient({}))
    assert len(calls) == len(rs.LABELS) and doc["sealed"]
    before = len(calls)
    assert rd.collect_research(tmp_path, DAY, state, settings) == doc and len(calls) == before
    old = rd.collect_research(tmp_path, "2026-10-28", state, settings)
    assert old["status"] == "historical_archive_missing" and len(calls) == before
    assert not (tmp_path / "research/data/2026-10-28.json").exists()
    path = tmp_path / f"research/data/{DAY}.json";bad = read_json(path);bad["symbols"] = []
    write_json(path, bad)
    with pytest.raises(ValueError, match="哈希"): rd.collect_research(tmp_path, DAY, state, settings)


def test_budget_failure_does_not_fabricate_zero_or_leak_exceptions(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(rd, "now", lambda: STAMP)
    def fail(*a): raise rs.FeedError("budget_exhausted")
    monkeypatch.setattr(rd, "fetch", fail)
    doc = rd.collect_research(tmp_path, DAY, {"positions": [{"code": "600519"}]}, settings, FakeClient({}))
    assert doc["datasets"]["limit_up"]["row_count"] is None
    facts = rd.add_to_facts({"limitations": ["没有提供新闻或公司公告，不得编造归因"]}, doc)
    assert any("缺少" in x for x in facts["limitations"])


def test_before_close_and_disabled_do_not_call_network_or_freeze(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(rd, "now", lambda: STAMP.replace(hour=14))
    monkeypatch.setattr(rd, "fetch", lambda *a: pytest.fail("outside collection window"))
    out = rd.collect_research(tmp_path, DAY, {}, settings)
    assert out["status"] == "before_research_collection_window"
    settings["enabled"] = False
    assert rd.collect_research(tmp_path, DAY, {}, settings)["status"] == "disabled"
    assert not (tmp_path / "research/data").exists()


def test_empty_portfolio_skips_company_requests_and_preserves_symbol_priority(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(rd, "now", lambda: STAMP)
    def fetch(client, kind, *a):
        assert kind not in ("dragon_tiger_seats", "announcements", "financials")
        return rs.result([], 0)
    monkeypatch.setattr(rd, "fetch", fetch)
    out = rd.collect_research(tmp_path, DAY, {}, settings, FakeClient({}))
    assert all(out["datasets"][k]["status"] == "not_applicable" for k in ("dragon_tiger_seats", "announcements", "financials"))
    state = {"positions": [{"code": "600519"}], "settlements": {DAY: {"sells": [{"code": "600000"}]}},
             "plans": {"next": {"data_date": DAY, "picks": [{"code": "000001"}]}}}
    assert rd.focus_symbols(state, DAY, 2) == (["600519", "600000"], 3)


def test_malformed_optional_feed_is_missing_but_other_provider_continues(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(rd, "now", lambda: STAMP)
    def fetch(client, kind, *a):
        if kind != "themes": raise AttributeError("PRIVATE response")
        return rs.result([{"code": "600000"}], 1)
    monkeypatch.setattr(rd, "fetch", fetch)
    out = rd.collect_research(tmp_path, DAY, {}, settings, FakeClient({}))
    assert out["datasets"]["themes"]["status"] == "ok"
    assert out["datasets"]["limit_up"]["reason"] == "invalid_schema"
    assert "PRIVATE" not in json.dumps(out)


def test_summary_is_bounded_and_marks_truncation():
    doc = {"date": DAY, "symbols": [], "datasets": {k: {"label": v, **rs.result([{"code": "600519", "title": "文" * 200} for _ in range(500)], 500)} for k,v in rs.LABELS.items()}}
    out = rd.summary(doc, 10000)
    assert len(json.dumps(out, ensure_ascii=False)) < 10000
    assert all(x["sample_truncated"] for x in out["datasets"].values())


def test_pipeline_roles_receive_evidence_and_resume_frozen_without_recollect(tmp_path, monkeypatch, cfg, cal, state):
    day = "2026-10-19"
    write_json(tmp_path / "account.json", state)
    m = market(day);write_json(tmp_path / "snapshots" / f"{day}.json", m)
    monkeypatch.setattr(cli, "collect", lambda *a: m)
    monkeypatch.setattr(cli, "scan_database", lambda *a: {"hits": [pick()], "pool": [pick()]})
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 10, 19, 20, 37, tzinfo=SHANGHAI))
    doc = {"date": day, "datasets": {k: {"label": v, **rs.result([], 0)} for k,v in rs.LABELS.items()}}
    monkeypatch.setattr(cli, "collect_research", lambda *a: doc)
    calls = []
    monkeypatch.setattr(meeting, "chat_completion", fake_model(calls, fail_at=3))
    with pytest.raises(RuntimeError): cli.execute(tmp_path, day, cfg, cal)
    assert all("research_data" in x["facts"] for x in calls)
    account_hash = file_hash(tmp_path / "account.json")
    monkeypatch.setattr(cli, "collect_research", lambda *a: pytest.fail("must retain frozen research inputs"))
    monkeypatch.setattr(meeting, "chat_completion", fake_model([]))
    cli.execute(tmp_path, day, cfg, cal, review_only=True)
    assert file_hash(tmp_path / "account.json") == account_hash
    assert "研究数据来源" in (tmp_path / "reports" / f"{day}.md").read_text()


def test_publication_only_exposes_source_status_not_raw_evidence(tmp_path):
    root = tmp_path / "state";seed(root)
    p = root / "reports/2026-10-19.json";d = read_json(p)
    d["facts"]["research_data"] = {"datasets": {"financials": {"label": "财务三表摘要", "source": "eastmoney", "status": "partial", "row_count": 6,
        "rows": [{"private": "PRIVATE_RAW"}], "note": "PRIVATE_FULL_NOTE", "attempts": [{"error": "PRIVATE_ERROR"}]}}}
    write_json(p,d);out=tmp_path / "site";pages.build_site(root,out)
    content=(out / "days/2026-10-19.html").read_text()
    assert "研究数据来源" in content and "有限覆盖" in content and "PRIVATE" not in content
