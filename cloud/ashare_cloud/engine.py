"""Deterministic paper execution. All plan decisions predate the trading session."""
from __future__ import annotations

import copy
from decimal import Decimal, ROUND_HALF_UP
from .common import digest


def money(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_price(prev, pct):
    return money(Decimal(str(prev)) * (1 + Decimal(str(pct)) / 100))


def fee(amount, cfg, sell=False):
    return money(max(amount * cfg["commission_rate"], cfg["commission_min"]) +
                 amount * (cfg["transfer_rate"] + (cfg["stamp_rate"] if sell else 0)))


def verify_plan(plan):
    payload = {k: v for k, v in plan.items() if k != "hash"}
    if digest(payload) != plan.get("hash"):
        raise ValueError("冻结计划校验失败")
    if plan["data_date"] >= plan["trade_date"] or plan["frozen_at"][:10] >= plan["trade_date"]:
        raise ValueError("计划必须在交易日前冻结；禁止事后生成交易")


def validate_bar(bar, day):
    import math
    if not bar or bar.get("date") != day:
        raise ValueError(f"缺少 {day} 的精确日期行情")
    for k in ("open", "high", "low", "close", "prev_close"):
        x = bar.get(k)
        if not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
            raise ValueError(f"无效行情字段 {k}")
    if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]):
        raise ValueError("OHLC 不一致")


def build_plan(day, scan, state, cfg, calendar, frozen_at):
    trade = calendar.next(day)
    # Never allocate money that is still invested. The actual buy is also cash checked.
    held = {p["code"] for p in state["positions"]}
    closed = state.get("closed_trades", [])
    paused = len(closed) >= 10 and sum(t["return_pct"] for t in closed[:10]) / 10 < 0
    plan = {"trade_date": trade, "data_date": day, "frozen_at": frozen_at,
            "strategy_version": cfg["strategy_version"], "config": copy.deepcopy(cfg),
            "picks": [x for x in scan["hits"] if x["code"] not in held],
            "baseline": scan["pool"], "buy_paused": paused,
            "gate_c": {"passed": False, "reason": "外围人工条件未获可验证盘前证据，保守计为不通过"},
            "previous_plan_hash": state.get("last_plan_hash"),
            "cash_at_freeze": state["cash"]}
    plan["hash"] = digest(plan)
    verify_plan(plan)
    return plan


def simulate(state, plan, market, day, calendar):
    """Return a new account and settlement; never mutate the input."""
    if day in state["settlements"]:
        return copy.deepcopy(state), state["settlements"][day]
    if state.get("last_settled") and calendar.next(state["last_settled"]) != day:
        raise ValueError("存在未结算交易日；请先使用该日已归档行情补跑，不能跳日记账")
    if market["date"] != day:
        raise ValueError("行情日期不匹配")
    if plan:
        verify_plan(plan)
        if plan["trade_date"] != day:
            raise ValueError("计划日期不匹配")
    result = copy.deepcopy(state)
    bars, events, sells = market["bars"], [], []
    opening_cash = state["cash"]
    old_value = state.get("equity", opening_cash)
    positions = []
    held_at_open = {p["code"] for p in state["positions"]}
    # Sell proceeds are credited only after buying: no use of future closing cash at open.
    for original in state["positions"]:
        p = copy.deepcopy(original)
        b = bars.get(p["code"])
        validate_bar(b, day)
        cfg = p["config"]
        p["mark"] = b["close"]
        if day <= p["buy_date"]:
            raise ValueError("持仓日期次序错误")
        down = limit_price(b["prev_close"], -p["limit_pct"])
        up = limit_price(b["prev_close"], p["limit_pct"])
        price, reason = None, None
        if p.get("forced_hold"):
            if b["open"] > down + 0.005:
                price, reason = b["open"], "跌停滞留后下一可卖开盘"
        elif day >= p["exit_date"]:
            if b["close"] <= down + 0.005:
                p["forced_hold"] = True
            elif not p.get("z1_used") and b["close"] >= up - 0.005:
                p["z1_used"] = True
                p["exit_date"] = calendar.next(day)
                events.append({"code": p["code"], "action": "defer", "reason": "Z1 涨停顺延一次"})
            else:
                price, reason = b["close"], "隔日收盘或 Z1 顺延收盘"
        if price is None:
            positions.append(p)
            if p.get("forced_hold"):
                events.append({"code": p["code"], "action": "hold", "reason": "跌停可成交性不足，继续计入持仓市值"})
        else:
            gross = money(price * p["qty"])
            cost = fee(gross, cfg, sell=True)
            net = money(gross - cost)
            pnl = money(net - p["cost"])
            trade = {"code": p["code"], "name": p["name"], "buy_date": p["buy_date"],
                     "sell_date": day, "qty": p["qty"], "price": price, "fee": cost,
                     "net": net, "pnl": pnl, "return_pct": round(pnl / p["cost"] * 100, 6),
                     "reason": reason}
            sells.append(trade)
            result["closed_trades"].append(trade)
            events.append({"action": "sell", **trade})

    buys, rejects = [], []
    gate = {"A": False, "B": False, "C": False}
    if plan and plan["picks"] and not plan["buy_paused"]:
        cfg = plan["config"]
        idx = market["index"]
        validate_bar(idx, day)
        gate["A"] = (idx["open"] / idx["prev_close"] - 1) * 100 > cfg["index_open_min_pct"]
        band = 0
        for pick in plan["picks"]:
            b = bars.get(pick["code"])
            validate_bar(b, day)
            gap = (b["open"] / b["prev_close"] - 1) * 100
            band += 0 < gap <= cfg["max_chase_pct"]
        gate["B"] = band >= min(2, len(plan["picks"]))
        gate["C"] = bool(plan["gate_c"]["passed"])
        n = sum(gate.values())
        budget = cfg["per_pick"] if n == 3 else cfg["reduced_per_pick"] if n == 2 else 0
        for pick in plan["picks"]:
            code = pick["code"]
            b = bars[code]
            gap = (b["open"] / b["prev_close"] - 1) * 100
            reason = None
            # The live prior close must agree with the frozen unadjusted close.
            if abs(b["prev_close"] - pick["prev_close"]) > 0.011:
                reason = "除权或前收不一致，冻结价格失效"
            elif code in held_at_open:
                reason = "已有持仓，不重复建仓"
            elif "ST" in b.get("name", pick["name"]).upper() or "退" in b.get("name", pick["name"]):
                reason = "当日风险标识"
            elif budget <= 0:
                reason = "开盘门槛不足两项"
            elif gap <= 0:
                reason = "R2 竞价不高开"
            elif gap > cfg["max_chase_pct"]:
                reason = "R4 高开超过追高上限"
            auction = b.get("auction_amount")
            if reason is None and (not isinstance(auction, (int, float)) or auction <= 0):
                reason = "缺少可靠竞价成交额，无法判定 R0"
            qty = int(budget / b["open"] // 100) * 100
            if qty == 0 and b["open"] * 100 <= 1.5 * budget:
                qty = 100
            amount = money(qty * b["open"])
            cost = fee(amount, cfg) if qty else 0
            if reason is None and max(budget, amount) / auction > cfg["liquidity_max_ratio"]:
                reason = "R0 委托占竞价成交额超过上限"
            if reason is None and (qty == 0 or amount + cost > result["cash"]):
                reason = "可用现金或整手预算不足"
            if reason:
                rejects.append({"code": code, "name": pick["name"], "reason": reason})
                continue
            result["cash"] = money(result["cash"] - amount - cost)
            position = {"code": code, "name": pick["name"], "qty": qty,
                        "buy_date": day, "exit_date": calendar.next(day),
                        "buy_price": b["open"], "cost": money(amount + cost),
                        "limit_pct": pick["limit_pct"], "mark": b["close"],
                        "config": cfg, "plan_hash": plan["hash"], "z1_used": False,
                        "forced_hold": False}
            positions.append(position)
            buys.append({"code": code, "name": pick["name"], "qty": qty,
                         "price": b["open"], "fee": cost, "amount": amount})
    result["cash"] = money(result["cash"] + sum(x["net"] for x in sells))
    result["positions"] = positions
    mv = money(sum(p["qty"] * p["mark"] for p in positions))
    result["equity"] = money(result["cash"] + mv)
    result["last_settled"] = day
    # These controls are descriptive intraday benchmarks, not executable T+0 trades.
    controls = {}
    for label, picks in (("shadow", plan["picks"] if plan else []),
                         ("limit_up_pool", plan["baseline"] if plan else [])):
        returns = []
        for p in picks:
            b = bars.get(p["code"])
            if b and b["open"] < limit_price(b["prev_close"], p["limit_pct"]) - 0.005:
                returns.append((b["close"] / b["open"] - 1) * 100)
        controls[label] = {"n": len(returns), "expected_n": len(picks),
                           "return_pct": sum(returns) / len(returns) if returns else None,
                           "basis": "开盘到收盘等权、未扣费、非可执行 T+0 收益"}
    settlement = {"date": day, "plan_hash": plan["hash"] if plan else None,
                  "status": "settled" if plan else "bootstrap_no_prior_plan",
                  "gate": gate, "gate_c_reason": plan["gate_c"]["reason"] if plan else "未建计划",
                  "buy_paused": bool(plan and plan["buy_paused"]),
                  "buys": buys, "sells": sells, "rejects": rejects, "events": events,
                  "cash": result["cash"], "market_value": mv, "equity": result["equity"],
                  "day_pnl": money(result["equity"] - old_value),
                  "realized_pnl": money(sum(t["pnl"] for t in sells)),
                  "positions": copy.deepcopy(positions), "controls": controls}
    result["settlements"][day] = settlement
    return result, settlement
