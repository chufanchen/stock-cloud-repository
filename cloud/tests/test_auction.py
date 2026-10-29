import copy
from datetime import datetime, timedelta

import pytest

from ashare_cloud import auction as a, cli, market as m
from ashare_cloud.common import SHANGHAI, read_json, write_json, validate_config
from ashare_cloud.engine import simulate
from ashare_cloud.storage import Store
from test_engine import cfg, cal, state, plan, market
from test_storage import FakeS3

DAY = "2026-10-20"


def raw(code="600001"):
    return {"1": "测试", "2": code, "3": "10.20", "4": "10", "5": "10.20",
            "6": "1000", "9": "10.20", "10": "100000000", "19": "10.21",
            "20": "0", "30": "20261020092500", "37": "102.00"}


def sample(second=5, **fields):
    quote = raw()
    quote.update(fields)
    return {"captured_at": f"2026-10-20T09:25:{second:02d}+08:00", "raw": quote}


def record(p):
    return {"schema": 1, "source": a.SOURCE, "date": DAY, "plan_hash": p["hash"],
            "samples": {"sh600001": [sample(), sample(8)]}}


def save_record(root, cfg, cal, state):
    p = plan(cfg, cal, state)
    state = copy.deepcopy(state)
    state["plans"][DAY] = p
    state["last_plan_hash"] = p["hash"]
    write_json(root / "account.json", state)
    write_json(root / f"auctions/{DAY}.json", record(p))
    return p


def test_units_confirmed_amount_and_book_volume_not_used(cfg, cal, state):
    p = plan(cfg, cal, state)
    result, rejected = a.validate_capture(record(p), p, DAY)
    assert not rejected and result["600001"]["amount"] == 1020000
    assert result["600001"]["inferred"] is True
    r = record(p)
    for s in r["samples"]["sh600001"]:
        s["raw"]["10"] = "999999999999"
        s["raw"]["37"] = "101.99"
    result, _ = a.validate_capture(r, p, DAY)
    assert result["600001"]["amount"] == 1019900


@pytest.mark.parametrize("stamp,captured", [
    ("20261020092459", "2026-10-20T09:25:05+08:00"),
    ("20261020093000", "2026-10-20T09:30:01+08:00"),
    ("20261019150000", "2026-10-20T09:25:05+08:00"),
    ("20261020092506", "2026-10-20T09:25:05+08:00"),
    ("20261020092500", "2026-10-20T09:25:05"),
    ("20261020092500", "2026-10-21T09:25:05+08:00"),
    ("20261020092500", "2026-10-20T09:29:51+08:00"),
])
def test_reject_virtual_old_future_or_after_continuous_quotes(stamp, captured):
    s = sample(**{"30": stamp})
    s["captured_at"] = captured
    with pytest.raises(ValueError):
        a.sample_value(s, "sh600001", DAY)


@pytest.mark.parametrize("field,value", [("5", "10.1"), ("2", "600002"), ("6", "0"),
    ("37", "1020000"), ("37", "NaN"), ("6", "Infinity"), ("30", "2026102009250")])
def test_bad_fields_and_unit_multipliers_rejected(field, value):
    with pytest.raises(ValueError):
        a.sample_value(sample(**{field: value}), "sh600001", DAY)


def test_two_real_observations_required_and_unstable_quotes_rejected():
    for samples in ([sample()], [sample(), sample()], [sample(), sample(45)],
                    [sample(), sample(8, **{"6": "1001", "37": "102.102"})]):
        with pytest.raises(ValueError):
            a.confirmed(samples, "sh600001", DAY)


def test_capture_must_match_frozen_plan_and_previous_close(cfg, cal, state):
    p = plan(cfg, cal, state)
    r = record(p)
    r["plan_hash"] = "wrong"
    with pytest.raises(ValueError):
        a.validate_capture(r, p, DAY)
    r = record(p)
    for s in r["samples"]["sh600001"]:
        s["raw"]["4"] = "9.9"
    result, rejected = a.validate_capture(r, p, DAY)
    assert not result and "600001" in rejected
    r = record(p)
    r["samples"]["sz000001"] = [sample(), sample(8)]
    with pytest.raises(ValueError):
        a.validate_capture(r, p, DAY)


def test_old_watch_auction_archive_is_not_execution_evidence(cfg, cal, state):
    with pytest.raises(ValueError):
        a.validate_capture({"date": "20261020", "codes": {"600001": {"ticks": []}}}, plan(cfg, cal, state), DAY)


def test_bse_prefix_and_parser_uses_provider_timestamp():
    assert [a.symbol(c) for c in ("600001", "000001", "920002", "430001", "830001")] == [
        "sh600001", "sz000001", "bj920002", "bj430001", "bj830001"]
    fields = [""] * 50
    for k, v in raw("920002").items():
        fields[int(k)] = v
    body = ('v_bj920002="' + "~".join(fields) + '";').encode("gbk")
    result = a.parse_quotes(body, ["bj920002"])
    assert result["bj920002"]["30"] == "20261020092500"
    assert result["bj920002"]["37"] == "102.00"


class Clock:
    def __init__(self, stamp="2026-10-20T09:24:59+08:00"):
        self.value = datetime.fromisoformat(stamp)
    def now(self):
        return self.value
    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


def test_capture_waits_for_match_then_saves_two_samples(tmp_path, cfg, cal, state):
    clock, calls = Clock(), []
    def fetch(symbols):
        calls.append(clock.value)
        assert symbols == ["sh600001"]
        return {"sh600001": raw()}
    r = a.capture(plan(cfg, cal, state), DAY, tmp_path / "record.json",
                  fetch=fetch, clock=clock.now, sleep=clock.sleep)
    assert r["status"] == "complete" and r["validated_count"] == 1
    assert len(calls) == 2 and calls[0].strftime("%H%M%S") == "092505"
    assert read_json(tmp_path / "record.json") == r


def test_slow_response_crossing_window_is_never_used(tmp_path, cfg, cal, state):
    clock = Clock("2026-10-20T09:29:49+08:00")
    def fetch(symbols):
        clock.sleep(12)
        return {"sh600001": raw()}
    r = a.capture(plan(cfg, cal, state), DAY, tmp_path / "record.json",
                  fetch=fetch, clock=clock.now, sleep=clock.sleep)
    assert r["status"] == "partial" and not r["samples"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_denial_stops_collector_without_repeated_requests(tmp_path, cfg, cal, state, status):
    clock, calls = Clock("2026-10-20T09:25:05+08:00"), []
    def fetch(symbols):
        calls.append(symbols)
        raise a.CaptureError(f"http_{status}")
    r = a.capture(plan(cfg, cal, state), DAY, tmp_path / "record.json",
                  fetch=fetch, clock=clock.now, sleep=clock.sleep)
    assert len(calls) == 1 and not r["samples"]


def test_cannot_capture_past_day_or_late_start(tmp_path, cfg, cal, state):
    with pytest.raises(ValueError):
        a.capture(plan(cfg, cal, state), DAY, tmp_path / "record.json", clock=Clock("2026-10-20T15:00:00+08:00").now)
    assert not (tmp_path / "record.json").exists()


def test_r2_separate_immutable_evidence_then_restore(tmp_path, cfg, cal, state):
    client = FakeS3()
    store = Store(client, "bucket", "project")
    p = plan(cfg, cal, state)
    assert a.publish_capture(store, record(p), p, DAY)
    assert list(client.files) == [a.capture_key(store, DAY, p)]
    with pytest.raises(RuntimeError, match="禁止覆盖"):
        a.publish_capture(store, record(p), p, DAY)
    a.restore_capture(store, tmp_path, p, DAY)
    assert read_json(tmp_path / f"auctions/{DAY}.json") == record(p)
    assert not any("HEAD" in k or "databases" in k or "states" in k for k in client.files)


def test_cloud_account_uses_small_bundle_without_database(tmp_path, state):
    (tmp_path / "market.db").write_bytes(b"fixture database")
    write_json(tmp_path / "account.json", state)
    store = Store(FakeS3(), "bucket", "project")
    store.push(tmp_path, bootstrap=True)
    def no_database(*args, **kwargs):
        pytest.fail("must not download database")
    store.client.download_file = no_database
    assert a.read_cloud_account(store) == state


@pytest.mark.parametrize("api", ["missing", "denied", "wrong_price", "valid"])
def test_paid_source_fallback_and_priority(tmp_path, monkeypatch, cfg, cal, state, api):
    p = save_record(tmp_path, cfg, cal, state)
    def tushare(day):
        if api == "denied":
            raise RuntimeError("provider response must not appear")
        if api == "missing":
            return {}, "missing token"
        return {"600001": {"price": 9 if api == "wrong_price" else 10.2,
                            "amount": 2000000, "source": "tushare_stk_auction_o"}}, "paid source"
    monkeypatch.setattr(m, "tushare_auction_amounts", tushare)
    values, note = m.auction_amounts(DAY, tmp_path, cfg, market()["bars"])
    assert "provider response" not in note
    assert values["600001"]["source"] == ("tushare_stk_auction_o" if api == "valid" else a.SOURCE)
    # Feed the chosen evidence to the existing R0 engine, without changing its rule.
    close = market(auction=None)
    close["bars"]["600001"]["auction_amount"] = values["600001"]["amount"]
    _, settled = simulate(state, p, close, DAY, cal)
    assert len(settled["buys"]) == 1


def test_missing_evidence_or_bad_open_remains_missing(tmp_path, monkeypatch, cfg, cal, state):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    rows, _ = m.auction_amounts(DAY, tmp_path, cfg)
    assert not rows
    save_record(tmp_path, cfg, cal, state)
    rows, _ = m.auction_amounts(DAY, tmp_path, cfg, market(opening=10.3)["bars"])
    assert not rows


def test_priority_config_rejects_unknown_source(cfg):
    cfg["auction_sources"] = ["minute_bar"]
    with pytest.raises(ValueError):
        validate_config(cfg)


def test_corrupt_evidence_does_not_turn_optional_source_into_pipeline_error(tmp_path, monkeypatch, cfg, cal, state):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    save_record(tmp_path, cfg, cal, state)
    write_json(tmp_path / f"auctions/{DAY}.json", [])
    rows, note = m.auction_amounts(DAY, tmp_path, cfg)
    assert not rows and "不合格" in note


def test_daily_reads_capture_before_collect_but_never_for_already_settled(tmp_path, monkeypatch, cfg, cal, state):
    p = save_record(tmp_path, cfg, cal, state)
    (tmp_path / f"auctions/{DAY}.json").unlink()
    store = Store(FakeS3(), "bucket", "project")
    a.publish_capture(store, record(p), p, DAY)
    def collect(root, day, *args, **kwargs):
        assert read_json(root / f"auctions/{day}.json") == record(p)
        close = market(auction=None)
        values, close["auction_note"] = m.auction_amounts(day, root, cfg, close["bars"])
        close["bars"]["600001"]["auction_amount"] = values["600001"]["amount"]
        write_json(root / f"snapshots/{day}.json", close)
        return close
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "scan_database", lambda *args: {"hits": [], "pool": []})
    monkeypatch.setattr(cli, "now", lambda: datetime(2026, 10, 20, 20, 37, tzinfo=SHANGHAI))
    store.push = lambda *args: None
    cli.execute(tmp_path, DAY, cfg, cal, store=store, skip_ai=True)
    saved = read_json(tmp_path / "account.json")
    assert len(saved["settlements"][DAY]["buys"]) == 1
    monkeypatch.setattr(a, "restore_capture", lambda *args: pytest.fail("never refetch settled evidence"))
    cli.execute(tmp_path, DAY, cfg, cal, store=store, skip_ai=True)
    assert read_json(tmp_path / "account.json") == saved
