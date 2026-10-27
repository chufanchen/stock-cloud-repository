from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(SHANGHAI)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


class Calendar:
    def __init__(self):
        import exchange_calendars as xc
        self.cal = xc.get_calendar("XSHG")

    def check(self, day):
        if not (str(self.cal.first_session.date()) <= day <= str(self.cal.last_session.date())):
            raise ValueError("日期超出 XSHG 日历覆盖范围，请更新 exchange-calendars；禁止按工作日猜测。")

    def is_session(self, day):
        self.check(day)
        return bool(self.cal.is_session(day))

    def next(self, day):
        self.check(day)
        return str(self.cal.next_session(day).date())

    def previous(self, day):
        self.check(day)
        return str(self.cal.previous_session(day).date())


def validate_config(c):
    for k in ("initial_cash", "per_pick", "reduced_per_pick", "commission_min"):
        if not isinstance(c[k], (int, float)) or c[k] <= 0:
            raise ValueError(f"无效配置 {k}")
    if c["lookback"] < 120 or not 0 < c["position_threshold"] < 1:
        raise ValueError("无效回看窗口或区间位门槛")
    if not 0.9 <= c["minimum_market_coverage"] <= 1:
        raise ValueError("市场覆盖率门槛不得低于 90%")
    if not 0 < c["liquidity_max_ratio"] <= 0.05 or not 0 < c["max_chase_pct"] <= 7:
        raise ValueError("无效成交约束")
    for key, default, lower, upper in (("fetch_workers", 2, 1, 8), ("history_attempts", 4, 1, 6),
                                      ("history_budget_seconds", 2400, 1, 2400),
                                      ("history_checkpoint_every", 500, 1, 1000),
                                      ("history_failure_limit", 8, 1, 30)):
        value = c.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"无效抓取配置 {key}")
    if c.get("history_source", "sina") not in {"sina", "eastmoney", "tencent"}:
        raise ValueError("history_source 必须是 sina、eastmoney 或 tencent")
    interval = c.get("history_min_interval", 0.35)
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 0.2 <= interval <= 5:
        raise ValueError("history_min_interval 必须为 0.2 到 5 秒")
    return c
