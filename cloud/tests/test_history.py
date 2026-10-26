import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from ashare_cloud import cli, market
from ashare_cloud.common import SHANGHAI, read_json, validate_config, write_json
from ashare_cloud.history import HistoryClient, HistoryFetchError, HistoryPaused, Pacer, parse_rows, retry_after
from ashare_cloud.storage import Store
from test_engine import cal, cfg, state
from test_storage import FakeS3


class Clock:
    current = 0.0

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += seconds


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def payload(symbol="sh600001"):
    return {"code": 0, "data": {symbol: {"day": [["2026-10-23", "10", "11", "11", "9", "100"]]}}}


def test_global_spacing_cooldown_and_stop():
    clock = Clock()
    pacer = Pacer(0.35, 100, clock.now, clock.sleep)
    starts = []
    for _ in range(4):
        pacer.wait()
        starts.append(clock.now())
    assert starts == pytest.approx([0, 0.35, 0.7, 1.05])
    pacer.failed(10)
    pacer.wait()
    assert clock.now() >= 11.05
    assert pacer.interval > pacer.base
    pacer.stop("test_stop")
    with pytest.raises(HistoryPaused, match="test_stop"):
        pacer.wait()


def test_429_retry_after_is_shared_and_alternate_host_used(monkeypatch):
    clock, calls = Clock(), []
    pacer = Pacer(0.35, 100, clock.now, clock.sleep)
    responses = iter([Response(429, headers={"Retry-After": "40"}), Response(payload=payload())])
    def get(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        calls.append((clock.now(), url))
        return next(responses)
    client = HistoryClient(pacer=pacer, get=get)
    assert client.history("sh600001", 12)[0][1] == "2026-10-23"
    assert calls[1][0] - calls[0][0] >= 40
    assert "proxy.finance.qq.com" in calls[0][1] and "web.ifzq.gtimg.cn" in calls[1][1]


def test_retry_after_http_date_and_budget():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    assert 88 <= retry_after(future) <= 90
    assert retry_after("nan") == retry_after("garbage") == 0
    clock = Clock()
    pacer = Pacer(0.35, 5, clock.now, clock.sleep)
    client = HistoryClient(pacer=pacer, get=lambda *a, **k: Response(429, headers={"Retry-After": "60"}))
    with pytest.raises(HistoryPaused, match="time_budget"):
        client.history("sh600001", 12)
    assert clock.now() == 5


@pytest.mark.parametrize("error,reason", [(requests.Timeout("secret-url"), "timeout"),
                                         (requests.ConnectionError("secret-url"), "connection_error")])
def test_network_failure_bounded_and_sanitized(error, reason):
    clock, attempts = Clock(), []
    def get(*args, **kwargs):
        attempts.append(clock.now())
        raise error
    client = HistoryClient({"history_attempts": 3}, Pacer(0.35, 100, clock.now, clock.sleep), get)
    with pytest.raises(HistoryFetchError) as caught:
        client.history("sh600001", 12)
    assert len(attempts) == 3 and attempts[2] - attempts[1] >= 4
    assert caught.value.reason == reason
    assert "secret-url" not in str(caught.value) + json.dumps(caught.value.failures)


@pytest.mark.parametrize("raw", [None, {}, {"data": "bad"},
    {"data": {"sh600001": {"day": [["2026-10-23", "nan", "1", "1", "1", "1"]]}}},
    {"data": {"sh600001": {"day": [["2026-10-23", "10", "11", "9", "8", "1"]]}}},
    {"data": {"sh600001": {"qfqday": [["2026-10-23", "1", "1", "1", "1", "1"]]}}}])
def test_bad_history_never_accepted(raw):
    with pytest.raises(ValueError):
        parse_rows(raw, "sh600001")


class Provider:
    def __init__(self, fail=(), day="2026-10-26"):
        self.fail, self.day, self.requested = set(fail), day, []

    def quotes(self, symbols):
        return {symbol: {"date": self.day, "timestamp": self.day.replace("-", "") + "150100",
                         "name": "测试", "open": 10, "close": 10, "high": 10, "low": 10,
                         "vol": 100, "prev_close": 10} for symbol in symbols}

    def history(self, symbol, count):
        self.requested.append(symbol)
        if symbol in self.fail:
            raise HistoryFetchError(symbol, [{"attempt": 1, "host": "test.invalid", "reason": "http_429"}])
        return [(symbol[2:], day, 10, 10, 10, 10, 100) for day in
                ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]]


def seed(root, state):
    root.mkdir(exist_ok=True)
    con = sqlite3.connect(root / "market.db")
    con.executescript(market.SCHEMA)
    for n in range(1, 5):
        code = f"60000{n}"
        con.execute("INSERT INTO stock VALUES(?,?,?,?,?)", (code, "测试", "sh", "sh", 10))
        con.execute("INSERT INTO day VALUES(?,?,?,?,?,?,?)", (code, "2026-10-19", 10, 10, 10, 10, 100))
    con.commit()
    con.close()
    write_json(root / "account.json", state)


def configure(monkeypatch, cfg):
    cfg.update(minimum_universe_size=1, fetch_workers=1, history_checkpoint_every=2, history_failure_limit=1)
    monkeypatch.setattr(market, "now", lambda: datetime(2026, 10, 26, 20, 37, tzinfo=SHANGHAI))
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)


def test_r2_resume_only_missing_history_without_settlement(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    root = tmp_path / "first"
    seed(root, state)
    store = Store(FakeS3(), "bucket", "test")
    store.push(root, bootstrap=True)
    first = Provider(fail=["sh600003"])
    monkeypatch.setattr(market, "Tencent", lambda *a: first)
    monkeypatch.setattr(cli, "simulate", lambda *a: pytest.fail("incomplete data must not settle"))
    monkeypatch.setattr(cli, "run_meeting", lambda *a, **k: pytest.fail("must not invoke AI"))
    with pytest.raises(ValueError, match="历史日线补齐不足"):
        cli.execute(root, "2026-10-26", cfg, cal, store=store)
    assert first.requested == ["sh600001", "sh600002", "sh600003"]
    restored = tmp_path / "restored"
    store.pull(restored)
    assert read_json(restored / "account.json") == state
    assert read_json(restored / "snapshots/2026-10-26.json")["complete"] is False
    diag = read_json(restored / "diagnostics/history-2026-10-26.json")
    assert diag["successful"] == 2 and diag["failed"] == 1 and diag["pending"] == ["600004"]
    assert diag["failure_reasons"] == {"http_429": 1}
    second = Provider()
    snapshot = market.collect(restored, "2026-10-26", cfg, cal, second, lambda: store.push(restored))
    assert second.requested == ["sh600003", "sh600004"]
    assert snapshot["complete"] is True
    assert read_json(restored / "history-pending.json")["since"] == {}
    assert read_json(restored / "account.json") == state


def test_gap_survives_new_day_quote(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    with pytest.raises(ValueError):
        market.collect(tmp_path, "2026-10-26", cfg, cal, Provider(fail=["sh600003"]))
    # All codes now have an October 26 quote, but two still lack October 20–23.
    monkeypatch.setattr(market, "now", lambda: datetime(2026, 10, 27, 20, 37, tzinfo=SHANGHAI))
    provider = Provider(day="2026-10-27")
    market.collect(tmp_path, "2026-10-27", cfg, cal, provider)
    assert provider.requested == ["sh600003", "sh600004"]
    con = sqlite3.connect(tmp_path / "market.db")
    assert con.execute("SELECT COUNT(*) FROM day WHERE date='2026-10-23'").fetchone()[0] == 4
    con.close()


def test_storage_failure_propagates_and_stops_new_requests(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    def conflict():
        raise RuntimeError("R2 状态已被其他任务更新")
    with pytest.raises(RuntimeError, match="其他任务"):
        market.collect(tmp_path, "2026-10-26", cfg, cal, provider, conflict)
    assert provider.requested == ["sh600001", "sh600002"]
    assert read_json(tmp_path / "account.json") == state
    assert read_json(tmp_path / "snapshots/2026-10-26.json")["complete"] is False


def test_empty_unadjusted_history_cannot_mark_success(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    provider.history = lambda *a: []
    with pytest.raises(ValueError, match="历史日线补齐不足"):
        market.collect(tmp_path, "2026-10-26", cfg, cal, provider)
    assert read_json(tmp_path / "diagnostics/history-2026-10-26.json")["failure_reasons"] == {"no_historical_rows": 1}


def test_stale_response_keeps_original_gap_even_after_partial_insert(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    provider.history = lambda symbol, count: [(symbol[2:], "2026-10-22", 10, 10, 10, 10, 100)]
    with pytest.raises(ValueError):
        market.collect(tmp_path, "2026-10-26", cfg, cal, provider)
    remaining = read_json(tmp_path / "history-pending.json")["since"]
    assert remaining["600001"] == "2026-10-19"
    diagnostic = read_json(tmp_path / "diagnostics/history-2026-10-26.json")
    assert diagnostic["successful"] == 0 and diagnostic["failure_reasons"] == {"missing_previous_session": 1}


def test_successful_run_preserves_exact_quote_and_completeness_threshold(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    original = provider.history
    provider.history = lambda symbol, count: original(symbol, count) + [(symbol[2:], "2026-10-26", 9, 9, 9, 9, 100)]
    snapshot = market.collect(tmp_path, "2026-10-26", cfg, cal, provider)
    con = sqlite3.connect(tmp_path / "market.db")
    assert set(x[0] for x in con.execute("SELECT close FROM day WHERE date='2026-10-26'")) == {10}
    con.close()
    assert snapshot["complete"] and cfg["minimum_market_coverage"] == 0.97
    diagnostic = read_json(tmp_path / "diagnostics/history-2026-10-26.json")
    assert diagnostic["previous_coverage"] == 1 and diagnostic["status"] == "complete"


@pytest.mark.parametrize("key,value", [("fetch_workers", 0), ("history_attempts", 0),
                                      ("history_budget_seconds", 99999), ("history_min_interval", 0),
                                      ("history_checkpoint_every", 0), ("history_failure_limit", True)])
def test_invalid_fetch_settings(cfg, key, value):
    cfg[key] = value
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_workflow_uploads_failure_diagnostics():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load((Path(__file__).parents[2] / ".github/workflows/daily.yml").read_text())
    step = workflow["jobs"]["daily"]["steps"][-1]
    assert step["if"] == "always()"
    assert ".cloud-state/diagnostics/" in step["with"]["path"]
