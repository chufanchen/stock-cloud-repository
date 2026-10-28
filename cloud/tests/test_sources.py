import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
import requests

from ashare_cloud.common import SHANGHAI, validate_config, read_json
from ashare_cloud.history import HistoryClient, HistoryFetchError, HistoryPaused, Pacer
from ashare_cloud.sources import PriorityHistory, ProviderPacer, priority_quotes
from ashare_cloud.tickflow import SourceError, TickFlow, api_symbol, daily_rows, quote_row
from test_engine import cfg, cal, state
from test_history import Clock, Response, Provider, configure, seed


def stamp(day="2026-10-26", time="15:00:00"):
    return int(datetime.fromisoformat(day + "T" + time).replace(tzinfo=SHANGHAI).timestamp() * 1000)


def columns(days=("2026-10-23", "2026-10-26")):
    return {"timestamp": [stamp(day, "00:00:00") for day in days],
            "open": [10] * len(days), "close": [10] * len(days), "high": [10] * len(days),
            "low": [10] * len(days), "volume": [100] * len(days), "amount": [100000] * len(days)}


def quote(symbol="600001.SH", day="2026-10-26"):
    return {"symbol": symbol, "region": "CN", "timestamp": stamp(day), "open": 10,
            "last_price": 10, "high": 10, "low": 10, "prev_close": 10,
            "volume": 100, "amount": 100000, "ext": {"type": "cn_equity", "name": "测试股票"}}


def ready_client(monkeypatch, get):
    monkeypatch.setenv("TICKFLOW_API_KEY", "fixture-secret")
    clock = Clock()
    client = TickFlow(Pacer(1, 300, clock.now, clock.sleep), get=get)
    return client, clock


def test_batch_contract_raw_prices_lots_and_bounded_cache(monkeypatch):
    calls = []
    def get(url, **kw):
        calls.append(kw)
        return Response(payload={"data": {s: columns() for s in kw["params"]["symbols"].split(",")}})
    client, _ = ready_client(monkeypatch, get)
    jobs = [("sh600001", 10), ("sz000001", 30), ("bj920002", 20)]
    client.prepare(jobs, 2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda job: client.history(*job), jobs))
    assert len(calls) == 2
    assert calls[0]["params"] == {"symbols": "600001.SH,000001.SZ", "period": "1d", "adjust": "none", "count": 30}
    assert calls[0]["headers"] == {"x-api-key": "fixture-secret"}
    assert calls[0]["allow_redirects"] is False
    assert rows[0][0] == ("600001", "2026-10-23", 10., 10., 10., 10., 100.)
    assert rows[2][0][0] == "920002" and client.cache == {}


@pytest.mark.parametrize("change", [
    lambda d: d["volume"].pop(),
    lambda d: d["timestamp"].__setitem__(0, d["timestamp"][1]),
    lambda d: d["timestamp"].__setitem__(0, d["timestamp"][0] // 1000),
    lambda d: d["volume"].__setitem__(0, 10000),
    lambda d: d["close"].__setitem__(0, 20),
    lambda d: d["amount"].__setitem__(0, float("nan")),
])
def test_reject_misaligned_duplicate_timestamp_bad_units_and_ohlc(change):
    data = columns()
    change(data)
    with pytest.raises(ValueError):
        daily_rows(data, "sh600001")


def test_quote_schema_timezone_name_units_and_no_auction():
    row = quote_row(quote(), "sh600001")
    assert row["timestamp"] == "20261026150000" and row["vol"] == 100
    assert "auction_amount" not in row and "amount" not in row
    with pytest.raises(ValueError):
        quote_row(quote("600002.SH"), "sh600001")
    assert api_symbol("sh000001") != api_symbol("sz000001")
    assert api_symbol("bj920002") == "920002.BJ"


def test_missing_key_never_requests(monkeypatch):
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    client = TickFlow(Pacer(), get=lambda *a, **k: pytest.fail("no key must not request"))
    with pytest.raises(SourceError, match="missing_key"):
        client.quotes(["sh600001"])


@pytest.mark.parametrize("status", [401, 403, 302])
def test_auth_failures_stop_and_redirects_never_follow(monkeypatch, status, capsys):
    calls = []
    def get(*a, **kw):
        calls.append(kw)
        response = Response(status, headers={"Location": "https://other.invalid/?key=private"})
        response.text = "fixture-secret private provider body"
        return response
    client, _ = ready_client(monkeypatch, get)
    for _ in range(2):
        with pytest.raises(SourceError):
            client.quotes(["sh600001"])
    assert len(calls) == 1
    assert "fixture-secret" not in json.dumps(client.statistics()) + capsys.readouterr().out
    if status in (401, 403):
        assert client.disabled == f"http_{status}"


def test_429_retry_after_cooldown_skips_source_without_waiting(monkeypatch):
    calls = []
    def get(*a, **kw):
        calls.append(kw)
        return Response(429, headers={"Retry-After": "120"}) if len(calls) == 1 else Response(payload={"data": [quote()]})
    client, clock = ready_client(monkeypatch, get)
    with pytest.raises(SourceError, match="http_429"):
        client.quotes(["sh600001"])
    with pytest.raises(SourceError, match="source_cooldown"):
        client.quotes(["sh600001"])
    assert clock.now() == 0 and len(calls) == 1
    clock.sleep(120)
    assert client.quotes(["sh600001"])
    assert len(calls) == 2


class FakeHistory:
    def __init__(self, rows=None, error=None, pacer=None):
        self.rows, self.error = rows, error
        self.pacer = pacer or Pacer()
        self.calls = 0

    def history(self, symbol, count):
        self.calls += 1
        if self.error:
            raise self.error
        return self.rows

    def statistics(self):
        return {"requests": self.calls, "attempt_errors": self.calls if self.error else 0, "recovered_symbols": 0}


@pytest.mark.parametrize("defect", ["prices", "previous", "symbol", "failure"])
def test_semantic_failure_tries_next_source_before_accepting(defect):
    good = daily_rows(columns(), "sh600001")
    bad = daily_rows(columns(), "sh600001")
    if defect == "prices":
        bad[-1] = ("600001", "2026-10-26", 9, 9, 9, 9, 100)
    elif defect == "previous":
        bad = bad[-1:]
    elif defect == "symbol":
        bad[0] = ("600002", *bad[0][1:])
    first = FakeHistory(bad, SourceError("timeout") if defect == "failure" else None)
    second = FakeHistory(good)
    wrapper = PriorityHistory({"history_sources": ["tickflow", "sina"]}, clients={"tickflow": first, "sina": second})
    wrapper.prepare([("sh600001", 10)], {"600001": quote_row(quote(), "sh600001")}, "2026-10-26", "2026-10-23")
    assert wrapper.history("sh600001", 10) == good
    assert wrapper.statistics()["selected_sources"] == {"sh600001": "sina"}
    assert wrapper.statistics()["recovered_symbols"] == 1


def test_waf_denial_does_not_retry_same_provider_and_fallback_works():
    clock, calls = Clock(), []
    parent = Pacer(1, 100, clock.now, clock.sleep)
    def get(url, **kw):
        calls.append(url)
        response = Response(302, headers={"Location": "https://waf.tencent.com/deny"})
        return response
    first = HistoryClient({"history_attempts": 1}, ProviderPacer(parent), get)
    second = FakeHistory(daily_rows(columns(), "sh600001"))
    wrapper = PriorityHistory({"history_sources": ["tencent", "sina"]}, parent,
                              {"tencent": first, "sina": second})
    for _ in range(2):
        assert wrapper.history("sh600001", 10)
    assert len(calls) == 1 and parent.stop_reason is None
    assert wrapper.statistics()["disabled"]["tencent"] == "access_denied"


def test_deadline_shared_across_provider_switches():
    clock = Clock()
    parent = Pacer(1, 3, clock.now, clock.sleep)
    first = FakeHistory(error=SourceError("timeout"), pacer=ProviderPacer(parent))
    second = FakeHistory([], pacer=ProviderPacer(parent))
    wrapper = PriorityHistory({"history_sources": ["tickflow", "sina"]}, parent,
                              {"tickflow": first, "sina": second})
    def slow_failure(*args):
        clock.sleep(3)
        raise SourceError("timeout")
    first.history = slow_failure
    with pytest.raises(HistoryPaused, match="time_budget"):
        wrapper.history("sh600001", 10)
    assert second.calls == 0


def test_quotes_only_fallback_missing_stale_or_invalid_symbols(monkeypatch):
    client, clock = ready_client(monkeypatch, lambda *a, **k: Response(payload={"data": [quote(), quote("600002.SH", "2026-10-23")]}))
    requested = []
    def legacy(symbols):
        requested.extend(symbols)
        return {s: {**quote_row(quote(api_symbol(s)), s), "source": "tencent_quote"} for s in symbols}
    result, stats = priority_quotes(["sh600001", "sh600002", "sh600003"],
                                  {"quote_sources": ["tickflow", "tencent"]}, legacy, "2026-10-26", client, client.pacer)
    assert requested == ["sh600002", "sh600003"]
    assert result["sh600001"]["source"] == "tickflow_quote"
    assert stats["selected_counts"] == {"tickflow": 1, "tencent": 2}


def test_missing_key_falls_back_and_preserves_incomplete_coverage(monkeypatch):
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    clock = Clock()
    pacer = Pacer(1, 10, clock.now, clock.sleep)
    result, stats = priority_quotes(["sh600001", "sh600002"], {"quote_sources": ["tickflow", "tencent"]},
                                   lambda ss: {ss[0]: quote_row(quote(), ss[0])}, "2026-10-26", pacer=pacer)
    assert set(result) == {"sh600001"} and stats["disabled"] == {"tickflow": "missing_key"}


def test_quote_429_also_cools_history_without_delaying_other_source(monkeypatch):
    calls = []
    def get(url, **kw):
        calls.append(url)
        return Response(429, headers={"Retry-After": "120"})
    tickflow, clock = ready_client(monkeypatch, get)
    parent = tickflow.pacer
    good = quote_row(quote(), "sh600001")
    result, _ = priority_quotes(["sh600001"], {"quote_sources": ["tickflow", "tencent"]},
                               lambda ss: {"sh600001": good}, "2026-10-26", tickflow, parent)
    second = FakeHistory(daily_rows(columns(), "sh600001"))
    wrapper = PriorityHistory({"history_sources": ["tickflow", "sina"]}, parent,
                              {"tickflow": tickflow, "sina": second})
    assert wrapper.history("sh600001", 10)
    assert len(calls) == 1 and clock.now() < 120 and result


def test_three_missing_stocks_do_not_disable_tickflow_batch(monkeypatch):
    rows = columns()
    client, clock = ready_client(monkeypatch, lambda *a, **k: Response(payload={"data": {"600004.SH": rows}}))
    jobs = [(f"sh60000{n}", 10) for n in range(1, 5)]
    wrapper = PriorityHistory({"history_sources": ["tickflow"]}, client.pacer, {"tickflow": client})
    wrapper.prepare(jobs, {s[2:]: quote_row(quote(api_symbol(s)), s) for s, _ in jobs}, "2026-10-26", "2026-10-23")
    for s, count in jobs[:3]:
        with pytest.raises(HistoryFetchError):
            wrapper.history(s, count)
    assert wrapper.history(*jobs[3]) and not wrapper.disabled
    assert client.statistics()["requests"] == 1


def test_repeated_network_errors_break_after_three_actual_requests(monkeypatch):
    def get(*a, **k):
        raise requests.Timeout("fixture-secret must never be logged")
    client, clock = ready_client(monkeypatch, get)
    for _ in range(4):
        with pytest.raises(SourceError):
            client.quotes(["sh600001"])
        clock.sleep(10)
    assert client.statistics()["requests"] == 3 and client.disabled == "circuit_open"


@pytest.mark.parametrize("key,value", [("history_sources", []), ("history_sources", ["sina", "sina"]),
    ("history_sources", ["unknown"]), ("quote_sources", ["sina"]), ("quote_sources", "tickflow"),
    ("tickflow_batch_size", 101), ("quote_budget_seconds", 9000)])
def test_invalid_priorities_rejected(cfg, key, value):
    cfg[key] = value
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_collect_retries_invalid_primary_before_marking_complete(tmp_path, monkeypatch, cfg, cal, state):
    from ashare_cloud import market
    configure(monkeypatch, cfg)
    seed(tmp_path, state)
    provider = Provider()
    # Stub the secondary source through the same public protocol as live backfill.
    class Secondary(FakeHistory):
        def history(self, symbol, count):
            self.calls += 1
            return Provider().history(symbol, count)
    first = FakeHistory(error=SourceError("http_403", permanent=True))
    second = Secondary()
    wrapper = PriorityHistory({"history_sources": ["tickflow", "sina"]}, clients={"tickflow": first, "sina": second})
    provider.history_client = wrapper
    provider.history = wrapper.history
    result = market.collect(tmp_path, "2026-10-26", cfg, cal, provider)
    assert result["complete"]
    diag = read_json(tmp_path / "diagnostics/history-2026-10-26.json")
    assert diag["network"]["disabled"] == {"tickflow": "http_403"}
    assert set(diag["network"]["selected_sources"].values()) == {"sina"}
    assert first.calls == 1 and second.calls == 4
    assert read_json(tmp_path / "account.json") == state
