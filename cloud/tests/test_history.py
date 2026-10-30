import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from ashare_cloud import cli, market
from ashare_cloud.common import SHANGHAI, read_json, validate_config, write_json
from ashare_cloud.history import SinaHistoryClient, EastmoneyHistoryClient, HistoryClient, HistoryFetchError, HistoryPaused, Pacer, parse_rows, retry_after
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


def test_remember_healthy_endpoint_and_count_recovered_failures():
    clock, hosts = Clock(), []
    def get(url, **kwargs):
        hosts.append(url)
        if "proxy.finance.qq.com" in url:
            return Response(502)
        return Response(payload=payload())
    client = HistoryClient(pacer=Pacer(0.35, 100, clock.now, clock.sleep), get=get)
    for _ in range(3):
        client.history("sh600001", 12)
    assert len(hosts) == 4
    assert sum("proxy.finance.qq.com" in url for url in hosts) == 1
    stats = client.statistics()
    assert stats["attempt_errors"] == stats["recovered_symbols"] == 1
    assert stats["requests"] == 4
    assert stats["hosts"]["proxy.finance.qq.com"]["errors"] == {"http_502": 1}
    assert stats["hosts"]["web.ifzq.gtimg.cn"]["successes"] == 3


def test_endpoint_latency_visible_even_without_errors():
    clock = Clock()
    def get(url, **kwargs):
        clock.sleep(4)
        return Response(payload=payload())
    client = HistoryClient(pacer=Pacer(0.35, 100, clock.now, clock.sleep), get=get)
    client.history("sh600001", 12)
    stats = client.statistics()
    assert stats["attempt_errors"] == 0
    assert stats["hosts"]["proxy.finance.qq.com"]["seconds"] == 4


def test_repeated_501_cooldown_bounded_but_retry_after_respected():
    from ashare_cloud.history import URLS
    clock = Clock()
    client = HistoryClient(pacer=Pacer(0.35, 2400, clock.now, clock.sleep))
    for _ in range(10):
        client._record(URLS[0], 0.1, "http_501")
        delay = client.health[URLS[0]]["blocked_until"] - clock.now()
        assert 0 < delay <= 30
        clock.sleep(delay)
    client._record(URLS[0], 0.1, "http_429", after=900)
    assert client.health[URLS[0]]["blocked_until"] - clock.now() >= 900


def test_501_discards_bad_persistent_connection(monkeypatch):
    clock, sessions = Clock(), []
    class Session:
        closed = False
        def __init__(self):
            self.index = len(sessions)
            sessions.append(self)
        def get(self, *args, **kwargs):
            return Response(501) if self.index == 0 else Response(payload=payload())
        def close(self):
            self.closed = True
    monkeypatch.setattr(requests, "Session", Session)
    client = HistoryClient(pacer=Pacer(0.35, 100, clock.now, clock.sleep))
    client.history("sh600001", 12)
    assert len(sessions) == 2 and sessions[0].closed
    assert client.statistics()["recovered_symbols"] == 1


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


def test_complete_previous_quotes_do_not_hide_older_gaps(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    con = sqlite3.connect(tmp_path / "market.db")
    con.executemany("INSERT INTO day VALUES(?,?,?,?,?,?,?)", [
        (f"60000{n}", "2026-10-23", 10, 10, 10, 10, 100) for n in range(1, 5)])
    con.commit()
    con.close()
    write_json(tmp_path / "history-pending.json", {"since": {f"60000{n}": "2026-10-19" for n in range(1, 5)}})
    with pytest.raises(ValueError, match="完整历史可用 2/4"):
        market.collect(tmp_path, "2026-10-26", cfg, cal, Provider(fail=["sh600003"]))
    diagnostic = read_json(tmp_path / "diagnostics/history-2026-10-26.json")
    assert diagnostic["previous_coverage"] == 1
    assert diagnostic["history_coverage"] == 0.5 and diagnostic["remaining_gaps"] == 2
    assert read_json(tmp_path / "snapshots/2026-10-26.json")["complete"] is False


def test_scanner_excludes_stocks_with_unresolved_history(tmp_path, cfg, cal, state):
    seed(tmp_path, state)
    con = sqlite3.connect(tmp_path / "market.db")
    for i in range(125):
        day = (datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat()
        con.execute("INSERT INTO day VALUES(?,?,?,?,?,?,?)", ("600001", day, 10, 10, 200, 9, 100))
    con.execute("INSERT INTO day VALUES(?,?,?,?,?,?,?)", ("600001", "2026-10-23", 10, 10, 10, 10, 100))
    con.execute("INSERT INTO day VALUES(?,?,?,?,?,?,?)", ("600001", "2026-10-26", 11, 11, 11, 11, 100))
    con.commit()
    con.close()
    assert len(market.scan_database(tmp_path / "market.db", "2026-10-26", cfg, cal)["hits"]) == 1
    write_json(tmp_path / "history-pending.json", {"since": {"600001": "2026-10-19"}})
    assert market.scan_database(tmp_path / "market.db", "2026-10-26", cfg, cal)["pool"] == []


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
                                      ("history_checkpoint_every", 0), ("history_failure_limit", True), ("history_source", "unknown")])
def test_invalid_fetch_settings(cfg, key, value):
    cfg[key] = value
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_actions_no_longer_fetches_history_or_uploads_private_artifacts():
    from pathlib import Path
    text = (Path(__file__).parents[2] / ".github/workflows/daily.yml").read_text()
    assert 'schedule:' not in text and 'upload-artifact' not in text
    assert 'AI_API_KEY' not in text and 'R2_SECRET_ACCESS_KEY' not in text


def test_archive_sync_resumes_during_next_day_without_quotes_auction_or_future_data(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    with pytest.raises(ValueError):
        market.collect(tmp_path, "2026-10-26", cfg, cal, Provider(fail=["sh600003"]))
    before = read_json(tmp_path / "snapshots/2026-10-26.json")
    monkeypatch.setattr(market, "now", lambda: datetime(2026, 10, 27, 9, 30, tzinfo=SHANGHAI))
    provider = Provider()
    provider.quotes = lambda *a: pytest.fail("sync must not use current quotes")
    original_history = provider.history
    provider.history = lambda symbol, count: original_history(symbol, count) + [(symbol[2:], "2026-10-27", 999, 999, 999, 999, 100)]
    monkeypatch.setattr(market, "auction_amounts", lambda *a: pytest.fail("sync must not fetch auctions"))
    result = market.collect(tmp_path, "2026-10-26", cfg, cal, provider, archive_only=True)
    assert provider.requested == ["sh600003", "sh600004"]
    assert result["complete"] and result["bars"] == before["bars"]
    assert result["captured_at"] == before["captured_at"]
    con = sqlite3.connect(tmp_path / "market.db")
    assert con.execute("SELECT COUNT(*) FROM day WHERE date>'2026-10-26'").fetchone()[0] == 0
    con.close()
    assert read_json(tmp_path / "account.json") == state


def test_sync_cli_selects_latest_archive_and_leaves_account_untouched(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    with pytest.raises(ValueError):
        market.collect(tmp_path, "2026-10-26", cfg, cal, Provider(fail=["sh600003"]))
    config_file = tmp_path / "test-config.json"
    write_json(config_file, cfg)
    monkeypatch.setattr(market, "now", lambda: datetime(2026, 10, 27, 9, 30, tzinfo=SHANGHAI))
    monkeypatch.setattr(market, "Tencent", lambda *a: Provider())
    monkeypatch.setattr(cli, "simulate", lambda *a: pytest.fail("sync must not settle"))
    monkeypatch.setattr(cli, "run_meeting", lambda *a: pytest.fail("sync must not call AI"))
    cli.main(["sync", "--local", "--state-dir", str(tmp_path), "--config", str(config_file)])
    assert read_json(tmp_path / "account.json") == state
    assert read_json(tmp_path / "snapshots/2026-10-26.json")["complete"]
    assert not (tmp_path / "reports").exists()


def test_archive_sync_requires_real_closed_snapshot(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    with pytest.raises(ValueError, match="收盘快照"):
        market.collect(tmp_path, "2026-10-26", cfg, cal, archive_only=True)
    provider = Provider()
    bars = provider.quotes(["sh600001", "sh600002", "sh600003", "sh600004", "sh000001"])
    for bar in bars.values():
        bar["timestamp"] = "20261026140000"
    write_json(tmp_path / "snapshots/2026-10-26.json", {"date": "2026-10-26", "complete": False,
               "captured_at": "2026-10-26T14:00:00+08:00", "index": bars.pop("sh000001"),
               "bars": {key[2:]: value for key, value in bars.items()}})
    with pytest.raises(ValueError, match="覆盖率"):
        market.collect(tmp_path, "2026-10-26", cfg, cal, provider, archive_only=True)


@pytest.mark.parametrize("status,body", [(403, "denied"), (501, '<script>location="https://waf.tencent.com/501page.html"</script>')])
def test_access_denial_stops_all_hosts_and_later_symbols(status, body):
    clock, calls = Clock(), []
    def get(url, **kwargs):
        calls.append(url)
        response = Response(status)
        response.text = body
        return response
    client = HistoryClient(pacer=Pacer(0.35, 100, clock.now, clock.sleep), get=get)
    for symbol in ("sh600001", "sh600002"):
        with pytest.raises(HistoryPaused, match="provider_access_denied"):
            client.history(symbol, 10)
    assert len(calls) == 1
    assert client.statistics()["attempt_errors"] == 1


@pytest.mark.parametrize("symbol,secid", [("sh688008", "1.688008"), ("sz000001", "0.000001"), ("bj920000", "0.920000")])
def test_eastmoney_unadjusted_identity_dates_and_volume(symbol, secid):
    clock = Clock()
    def get(url, **kwargs):
        assert "eastmoney.com" in url
        assert kwargs["params"]["secid"] == secid
        assert kwargs["params"]["fqt"] == "0"
        return Response(payload={"data": {"code": symbol[2:], "klines": ["2026-10-23,10,11,11,9,100,100000"]}})
    client = EastmoneyHistoryClient(pacer=Pacer(0.35, 100, clock.now, clock.sleep), get=get)
    assert client.history(symbol, 12) == [(symbol[2:], "2026-10-23", 10, 11, 11, 9, 100)]
    assert client.statistics()["source"] == "eastmoney"
    with pytest.raises(ValueError, match="symbol_mismatch"):
        client.parse({"data": {"code": "other", "klines": []}}, symbol)


def test_independent_source_price_mismatch_does_not_clear_gap(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    provider.history_client = EastmoneyHistoryClient()
    original = provider.history
    provider.history = lambda symbol, count: [tuple([x[0], x[1]] + [9, 9, 9, 9, 100]) for x in original(symbol, count)]
    with pytest.raises(ValueError, match="历史日线补齐不足"):
        market.collect(tmp_path, "2026-10-26", cfg, cal, provider)
    diag = read_json(tmp_path / "diagnostics/history-2026-10-26.json")
    assert diag["failure_reasons"] == {"archive_price_mismatch": 1}
    assert diag["remaining_gaps"] == 4
    con = sqlite3.connect(tmp_path / "market.db")
    assert con.execute("SELECT COUNT(*) FROM day WHERE date='2026-10-23'").fetchone()[0] == 0
    con.close()
    assert read_json(tmp_path / "account.json") == state


def test_independent_source_fills_only_gaps_and_retains_existing_bars(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    provider.history_client = EastmoneyHistoryClient()
    original = provider.history
    provider.history = lambda symbol, count: original(symbol, count) + [(symbol[2:], "2026-10-19", 9, 9, 9, 9, 90)]
    snapshot = market.collect(tmp_path, "2026-10-26", cfg, cal, provider)
    assert snapshot["complete"]
    con = sqlite3.connect(tmp_path / "market.db")
    assert {x[0] for x in con.execute("SELECT close FROM day WHERE date='2026-10-19'")} == {10}
    con.close()


def test_denial_checkpoints_pending_without_settlement(tmp_path, monkeypatch, cfg, cal, state):
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    provider.history = lambda *a: (_ for _ in ()).throw(HistoryPaused("provider_access_denied"))
    store = Store(FakeS3(), "bucket", "test")
    store.push(tmp_path, bootstrap=True)
    with pytest.raises(ValueError, match="行情源拒绝访问"):
        market.collect(tmp_path, "2026-10-26", cfg, cal, provider, lambda: store.push(tmp_path))
    restored = tmp_path / "restored"
    store.pull(restored)
    diag = read_json(restored / "diagnostics/history-2026-10-26.json")
    assert diag["stop_reason"] == "provider_access_denied" and diag["remaining_gaps"] == 4
    assert diag["status"] == "partial"
    assert read_json(restored / "account.json") == state


@pytest.mark.parametrize("symbol", ["sh688008", "sz000001", "bj920000"])
def test_sina_daily_jsonp_and_share_conversion(symbol):
    clock = Clock()
    def get(url, **kwargs):
        assert "quotes.sina.cn" in url
        assert kwargs["params"] == {"symbol": symbol, "scale": "240", "ma": "no", "datalen": "10"}
        response = Response()
        response.text = '/* JSONP */=(' + json.dumps([{"day": "2026-10-27", "open": "10", "close": "11", "high": "11", "low": "9", "volume": "12345"}]) + ');'
        return response
    client = SinaHistoryClient(pacer=Pacer(1, 100, clock.now, clock.sleep), get=get)
    assert client.history(symbol, 10) == [(symbol[2:], "2026-10-27", 10, 11, 11, 9, 123.45)]


@pytest.mark.parametrize("body", ['=([{day:"2026-10-27"}]);', '=([]);doSomething();', '<html>denied</html>'])
def test_sina_never_executes_jsonp_or_accepts_html(body):
    response = Response();response.text = body
    with pytest.raises(ValueError):
        SinaHistoryClient().decode(response)


def test_sina_rejects_intraday_rows():
    with pytest.raises(ValueError):
        SinaHistoryClient().parse([{"day": "2026-10-27 14:00:00", "open": 10, "close": 10, "high": 10, "low": 10, "volume": 100}], "sh600001")
