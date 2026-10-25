import copy
from datetime import datetime

import pytest

from ashare_cloud.common import Calendar, SHANGHAI, digest, read_json
from ashare_cloud.engine import build_plan, fee, limit_price, simulate, verify_plan


@pytest.fixture
def cfg():
    from pathlib import Path
    return read_json(Path(__file__).parents[1] / "config.json")


@pytest.fixture
def cal():
    return Calendar()


@pytest.fixture
def state():
    return {"schema": 1, "cash": 100013.62, "equity": 100013.62, "positions": [],
            "closed_trades": [], "last_settled": None, "settlements": {}, "plans": {},
            "last_plan_hash": None}


def pick(code="600001", price=10):
    return {"code": code, "name": "测试股票", "mkt": "sh", "limit_pct": 10,
            "prev_close": price, "pos": 0.08}


def market(day="2026-10-20", opening=10.2, close=10.3, prev=10, auction=1000000):
    b = {"date": day, "name": "测试股票", "open": opening, "close": close,
         "prev_close": prev, "high": max(opening, close), "low": min(opening, close),
         "auction_amount": auction}
    return {"date": day, "bars": {"600001": b}, "index": {**b, "open": 100, "prev_close": 100,
            "high": 100, "low": 100, "close": 100}, "coverage": 1, "auction_note": "测试数据"}


def plan(cfg, cal, state, picks=None):
    picks = [pick()] if picks is None else picks
    return build_plan("2026-10-19", {"hits": picks, "pool": picks}, state, cfg, cal,
                      "2026-10-19T20:37:00+08:00")


def test_calendar_holidays_and_next(cal):
    assert not cal.is_session("2026-10-01")
    assert not cal.is_session("2026-10-25")
    assert cal.next("2026-09-30") == "2026-10-08"
    assert cal.next("2026-10-23") == "2026-10-26"
    with pytest.raises(ValueError):
        cal.is_session("2050-01-03")


def test_buy_and_t1(cfg, cal, state):
    original = copy.deepcopy(state)
    s, result = simulate(state, plan(cfg, cal, state), market(), "2026-10-20", cal)
    assert state == original
    assert result["gate"] == {"A": True, "B": True, "C": False}
    assert result["buys"][0]["qty"] == 300
    assert not result["sells"]
    assert s["positions"][0]["exit_date"] == "2026-10-21"
    assert s["equity"] == round(s["cash"] + 300 * 10.3, 2)
    later, result = simulate(s, None, market("2026-10-21", 10.3, 10.4, 10.3), "2026-10-21", cal)
    assert not later["positions"]
    assert len(later["closed_trades"]) == 1
    assert result["realized_pnl"] > 0


def test_duplicate_run_no_new_trades(cfg, cal, state):
    p = plan(cfg, cal, state)
    s, first = simulate(state, p, market(), "2026-10-20", cal)
    same, second = simulate(s, p, market(close=1), "2026-10-20", cal)
    assert same == s and first == second


@pytest.mark.parametrize("opening,auction,reason", [(10, 1000000, "R2"), (10.8, 1000000, "R4"),
                                                    (10.2, None, "竞价"), (10.2, 100, "R0")])
def test_rejections(cfg, cal, state, opening, auction, reason):
    # Add one valid second candidate so the price-specific rejection is reached through gate B.
    picks = [pick(), pick("600002"), pick("600003")]
    m = market(opening=opening, auction=auction)
    m["bars"]["600002"] = market()["bars"]["600001"]
    m["bars"]["600003"] = market()["bars"]["600001"]
    _, result = simulate(state, plan(cfg, cal, state, picks), m, "2026-10-20", cal)
    assert any(reason in x["reason"] for x in result["rejects"] if x["code"] == "600001")


def test_no_candidates(cfg, cal, state):
    s, r = simulate(state, plan(cfg, cal, state, []), market(), "2026-10-20", cal)
    assert s["cash"] == state["cash"] and not r["buys"]


def test_bad_dates_and_modified_plan(cfg, cal, state):
    p = plan(cfg, cal, state)
    p["picks"][0]["prev_close"] = 9
    with pytest.raises(ValueError, match="校验"):
        simulate(state, p, market(), "2026-10-20", cal)
    p = plan(cfg, cal, state)
    m = market()
    m["bars"]["600001"]["date"] = "2026-10-19"
    with pytest.raises(ValueError, match="日期"):
        simulate(state, p, m, "2026-10-20", cal)
    state["last_settled"] = "2026-10-16"
    with pytest.raises(ValueError, match="未结算"):
        simulate(state, p, market(), "2026-10-20", cal)


def test_z1_once_and_limit_down_retains_value(cfg, cal, state):
    s, _ = simulate(state, plan(cfg, cal, state), market(), "2026-10-20", cal)
    s, r = simulate(s, None, market("2026-10-21", 10.4, 11.33, 10.3), "2026-10-21", cal)
    assert s["positions"][0]["z1_used"]
    s, r = simulate(s, None, market("2026-10-22", 12, 12.46, 11.33), "2026-10-22", cal)
    assert not s["positions"] and len(r["sells"]) == 1
    s, _ = simulate(state, plan(cfg, cal, state), market(), "2026-10-20", cal)
    s, r = simulate(s, None, market("2026-10-21", 9.27, 9.27, 10.3), "2026-10-21", cal)
    assert s["positions"][0]["forced_hold"] and r["market_value"] > 0
    s, r = simulate(s, None, market("2026-10-22", 8.34, 8.34, 9.27), "2026-10-22", cal)
    assert s["positions"]
    s, r = simulate(s, None, market("2026-10-23", 8.4, 8.5, 8.34), "2026-10-23", cal)
    assert not s["positions"] and r["sells"][0]["price"] == 8.4


def test_cash_and_existing_positions(cfg, cal, state):
    state["cash"] = 100
    state["equity"] = 100
    s, r = simulate(state, plan(cfg, cal, state), market(), "2026-10-20", cal)
    assert not r["buys"] and s["cash"] == 100
    assert any("现金" in x["reason"] for x in r["rejects"])


def test_one_lot_liquidity_uses_actual_amount(cfg, cal, state):
    p = plan(cfg, cal, state, [pick(price=40)])
    m = market(opening=41, close=41, prev=40, auction=75000)
    _, r = simulate(state, p, m, "2026-10-20", cal)
    # Budget3500/75000 <5%, but the actual one-lot4100/75000 >5%.
    assert not r["buys"] and "R0" in r["rejects"][0]["reason"]


def test_observation_pause_and_rounding(cfg, cal, state):
    state["closed_trades"] = [{"return_pct": -1}] * 10
    p = plan(cfg, cal, state)
    assert p["buy_paused"]
    _, r = simulate(state, p, market(), "2026-10-20", cal)
    assert not r["buys"]
    assert limit_price(6.75, 10) == 7.43
    assert fee(10000, cfg, sell=True) == 10.1


def test_cannot_freeze_retroactive_plan(cfg, cal, state):
    with pytest.raises(ValueError, match="交易日前"):
        build_plan("2026-10-19", {"hits": [], "pool": []}, state, cfg, cal,
                   "2026-10-20T20:00:00+08:00")
