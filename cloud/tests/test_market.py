import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from ashare_cloud import market as mod
from ashare_cloud.common import SHANGHAI, write_json
from test_engine import cal, cfg, market


def test_quotes_parse_date_and_market_prefix(monkeypatch):
    f = ["0"] * 53
    for i, value in {1: "指数", 3: "100", 4: "99", 5: "99.5", 6: "1000", 30: "20261019150000", 33: "101", 34: "99"}.items():
        f[i] = value
    class Response:
        content = ('v_sh000001="' + "~".join(f) + '";').encode("gbk")
        def raise_for_status(self):
            pass
    monkeypatch.setattr(mod.requests, "get", lambda *a, **k: Response())
    monkeypatch.setattr(mod.time, "sleep", lambda *a: None)
    q = mod.Tencent().quotes(["sh000001", "sz000001"])
    assert list(q) == ["sh000001"]
    assert q["sh000001"]["date"] == "2026-10-19"


def test_reject_adjusted_kline_fallback(monkeypatch):
    from ashare_cloud.history import parse_rows
    with pytest.raises(ValueError, match="adjusted_only"):
        parse_rows({"data": {"sh600001": {"qfqday": [["2026-10-19", "1", "1", "1", "1", "1"]]}}}, "sh600001")


def test_auction_date_and_units(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"code": 0, "data": {"fields": ["ts_code", "trade_date", "close", "vol", "amount", "vwap"],
                    "items": [["600001.SH", "20261019", 10, 1000, 10000, 10],
                              ["600002.SH", "20261019", 10, 1000, 10000000, 10],
                              ["600003.SH", "20261016", 10, 1000, 10000, 10]]}}
    monkeypatch.setattr(mod.requests, "post", lambda *a, **k: Response())
    rows, _ = mod.auction_amounts("2026-10-19")
    assert set(rows) == {"600001"}


def test_missing_auction_token_is_explicit(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    rows, note = mod.auction_amounts("2026-10-19")
    assert not rows and "无法判定" in note


def test_archived_market_and_no_stale_live_replay(tmp_path, cfg, cal):
    m = market("2026-10-19")
    m["complete"] = True
    write_json(tmp_path / "snapshots/2026-10-19.json", m)
    assert mod.collect(tmp_path, "2026-10-19", cfg, cal) == m
    with pytest.raises(ValueError, match="历史补跑"):
        mod.collect(tmp_path, "2026-10-16", cfg, cal)


def test_empty_pool_and_short_history(tmp_path, cfg, cal):
    db = tmp_path / "test.db"
    con = sqlite3.connect(db)
    con.executescript(mod.SCHEMA)
    con.execute("INSERT INTO stock VALUES('600001','测试','主板','sh',10)")
    con.executemany("INSERT INTO day VALUES(?,?,?,?,?,?,?)", [
        ("600001", "2026-10-16", 10, 10, 10, 10, 100),
        ("600001", "2026-10-19", 11, 11, 11, 11, 100)])
    con.commit()
    con.close()
    r = mod.scan_database(db, "2026-10-19", cfg, cal)
    assert not r["hits"]
