from __future__ import annotations

import math
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import requests

from .common import now, read_json, write_json
from .engine import limit_price, validate_bar

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
SCHEMA = """
CREATE TABLE stock(code TEXT PRIMARY KEY, name TEXT, board TEXT, mkt TEXT, limit_pct REAL);
CREATE TABLE day(code TEXT,date TEXT,open REAL,close REAL,high REAL,low REAL,vol REAL,PRIMARY KEY(code,date));
CREATE INDEX day_date ON day(date);
"""


def board_limit(code, name):
    if code.startswith(("8", "4", "92")):
        return "bj", 30.0
    mkt = "sh" if code.startswith(("6", "9")) else "sz"
    pct = 20.0 if code.startswith(("30", "68")) else 5.0 if "ST" in name.upper() else 10.0
    return mkt, pct


def seed_database(source, target, sessions=200):
    source = Path(source).resolve()
    con = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    rows = con.execute("SELECT DISTINCT date FROM day ORDER BY date DESC LIMIT ?", (sessions,)).fetchall()
    if len(rows) < 120:
        raise ValueError("源数据库不足 120 个交易日")
    cutoff = rows[-1][0]
    dest = sqlite3.connect(target)
    try:
        dest.executescript(SCHEMA)
        dest.executemany("INSERT INTO stock VALUES(?,?,?,?,?)",
                         con.execute("SELECT code,name,board,mkt,limit_pct FROM stock"))
        cursor = con.execute("SELECT code,date,open,close,high,low,vol FROM day WHERE date>=?", (cutoff,))
        while chunk := cursor.fetchmany(10000):
            dest.executemany("INSERT INTO day VALUES(?,?,?,?,?,?,?)", chunk)
        dest.commit()
        return {"seed_start": cutoff, "seed_end": rows[0][0],
                "stock_count": dest.execute("SELECT COUNT(*) FROM stock").fetchone()[0]}
    finally:
        con.close()
        dest.close()


class Tencent:
    def quotes(self, symbols):
        out = {}
        for start in range(0, len(symbols), 40):
            batch = symbols[start:start + 40]
            for attempt in range(3):
                try:
                    r = requests.get("https://qt.gtimg.cn/q=" + ",".join(batch), headers=HEADERS, timeout=20)
                    r.raise_for_status()
                    for line in r.content.decode("gbk", "replace").split(";"):
                        if '="' not in line:
                            continue
                        key, raw = line.split('="', 1)
                        sym = key.strip().removeprefix("v_")
                        fields = raw.rstrip('"\r\n ').split("~")
                        if len(fields) < 38 or len(fields[30]) < 14:
                            continue
                        try:
                            stamp = fields[30]
                            bar = {"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}", "timestamp": stamp,
                                   "name": fields[1], "close": float(fields[3]), "prev_close": float(fields[4]),
                                   "open": float(fields[5]), "vol": float(fields[6]),
                                   "high": float(fields[33]), "low": float(fields[34]), "source": "tencent_quote"}
                            if sym in batch:
                                out[sym] = bar
                        except (ValueError, IndexError):
                            continue
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise RuntimeError("腾讯行情批量请求失败；不提交不完整结算") from None
                    time.sleep(2 ** attempt)
            if start % 400 == 0:
                print(f"行情快照：{min(start + 40, len(symbols))}/{len(symbols)}", flush=True)
            time.sleep(0.12)
        return out

    def history(self, symbol, count):
        urls = ["https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"]
        for attempt in range(3):
            try:
                r = requests.get(urls[attempt % 2], params={"param": f"{symbol},day,,,{count},"},
                                 headers=HEADERS, timeout=20)
                r.raise_for_status()
                j = r.json()
                # Never accept qfqday as a fallback: limit prices require unadjusted data.
                rows = j.get("data", {}).get(symbol, {}).get("day", [])
                if not rows:
                    raise ValueError("empty")
                return [(symbol[2:], x[0], *map(float, x[1:6])) for x in rows if len(x) >= 6]
            except (requests.RequestException, ValueError, TypeError, KeyError):
                time.sleep(attempt + 0.2)
        raise RuntimeError(f"历史日线取数失败：{symbol}")


def auction_amounts(day):
    """Optional Tushare official post-close opening-auction endpoint (updates at 20:00).

    Do not substitute 09:30 first-minute turnover for auction turnover.
    """
    token = os.getenv("TUSHARE_TOKEN", "")
    if not token:
        return {}, "未配置 TUSHARE_TOKEN：没有可靠竞价成交额的候选将明确记为无法判定，不模拟成交"
    payload = {"api_name": "stk_auction_o", "token": token,
               "params": {"trade_date": day.replace("-", "")},
               "fields": "ts_code,trade_date,close,vol,amount,vwap"}
    for attempt in range(3):
        try:
            r = requests.post("https://api.tushare.pro", json=payload, timeout=35)
            r.raise_for_status()
            j = r.json()
            if j.get("code") != 0:
                raise RuntimeError("Tushare 集合竞价接口返回错误，请检查 stk_auction_o 权限")
            data = j.get("data") or {}
            out = {}
            for vals in data.get("items") or []:
                x = dict(zip(data["fields"], vals))
                if x.get("trade_date") != day.replace("-", ""):
                    continue
                amount, vol, vwap = x.get("amount"), x.get("vol"), x.get("vwap")
                if not all(isinstance(v, (float, int)) and math.isfinite(v) and v > 0 for v in (amount, vol, vwap)):
                    continue
                # Endpoint sample documents amount in yuan, vol in shares.
                # Check that unit changes cannot silently inflate liquidity by 100x/1000x.
                if abs(amount / vol / vwap - 1) > 0.02:
                    continue
                out[x["ts_code"].split(".")[0]] = {"amount": amount, "price": x["close"], "source": "tushare_stk_auction_o"}
            if not out:
                raise RuntimeError("集合竞价数据尚未发布或权限不足")
            return out, "Tushare stk_auction_o，成交额单位：元"
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise RuntimeError("Tushare 集合竞价数据请求失败") from None
            time.sleep(2 ** attempt)


def collect(root, day, cfg, calendar, provider=None):
    root = Path(root)
    cache = root / "snapshots" / f"{day}.json"
    if cache.exists():
        saved = read_json(cache)
        if saved.get("complete") and saved["date"] == day:
            return saved
    if day != now().date().isoformat() or now().hour < 15:
        raise ValueError("实时接口仅用于当天收盘后；历史补跑必须有该日归档快照")
    provider = provider or Tencent()
    con = sqlite3.connect(root / "market.db")
    con.row_factory = sqlite3.Row
    stocks = [dict(r) for r in con.execute("SELECT * FROM stock ORDER BY code")]
    if len(stocks) < cfg["minimum_universe_size"]:
        raise ValueError("股票池规模不足")
    previous = calendar.previous(day)
    quotes = provider.quotes([s["mkt"] + s["code"] for s in stocks] + ["sh000001"])
    bars, rejected = {}, []
    for s in stocks:
        b = quotes.get(s["mkt"] + s["code"])
        try:
            validate_bar(b, day)
            if b["timestamp"][8:12] < "1500":
                raise ValueError("尚未收盘")
            bars[s["code"]] = b
        except ValueError:
            rejected.append(s["code"])
    coverage = len(bars) / len(stocks)
    if coverage < cfg["minimum_market_coverage"]:
        raise ValueError(f"当日行情覆盖率 {coverage:.2%} 不足；可能停更或接口失败")
    index = quotes.get("sh000001")
    validate_bar(index, day)
    if index["timestamp"][8:12] < "1500":
        raise ValueError("上证指数尚未收盘")
    auctions, auction_note = auction_amounts(day)
    for code, a in auctions.items():
        if code in bars and abs(a["price"] - bars[code]["open"]) <= 0.011:
            bars[code]["auction_amount"] = a["amount"]
            bars[code]["auction_source"] = a["source"]
    # Persist raw observations before historical requests, so interruptions retain evidence locally.
    partial = {"date": day, "captured_at": now().isoformat(), "bars": bars, "index": index,
               "coverage": coverage, "missing_codes": rejected, "auction_note": auction_note,
               "complete": False}
    write_json(cache, partial)
    latest = dict(con.execute("SELECT code,MAX(date) FROM day GROUP BY code"))
    stale = [s for s in stocks if s["code"] in bars and latest.get(s["code"], "") < previous]
    errors = []
    with ThreadPoolExecutor(max_workers=cfg["fetch_workers"]) as pool:
        tasks = {}
        for s in stale:
            last = latest.get(s["code"])
            gap = (date.fromisoformat(day) - date.fromisoformat(last)).days if last else 700
            tasks[pool.submit(provider.history, s["mkt"] + s["code"], min(700, max(10, gap + 5)))] = s
        for n, task in enumerate(as_completed(tasks), 1):
            s = tasks[task]
            try:
                rows = task.result()
                rows = [x for x in rows if x[1] <= day]
                con.executemany("INSERT OR REPLACE INTO day VALUES(?,?,?,?,?,?,?)", rows)
            except RuntimeError:
                errors.append(s["code"])
            if n % 500 == 0:
                print(f"补齐缺失日线：{n}/{len(tasks)}", flush=True)
    # Today's exact unadjusted quotes are authoritative for the close and risk labels.
    for code, b in bars.items():
        con.execute("INSERT OR REPLACE INTO day VALUES(?,?,?,?,?,?,?)",
                    (code, day, b["open"], b["close"], b["high"], b["low"], b["vol"]))
        mkt, lp = board_limit(code, b["name"])
        con.execute("UPDATE stock SET name=?,mkt=?,limit_pct=? WHERE code=?", (b["name"], mkt, lp, code))
    con.commit()
    eligible_prev = con.execute("SELECT COUNT(*) FROM day WHERE date=?", (previous,)).fetchone()[0]
    con.close()
    if eligible_prev / len(stocks) < cfg["minimum_market_coverage"] or len(errors) / len(stocks) > 1 - cfg["minimum_market_coverage"]:
        raise ValueError("历史日线补齐不足，拒绝以不完整股票池选股")
    partial.update(complete=True, history_errors=errors)
    write_json(cache, partial)
    return partial


def scan_database(db, day, cfg, calendar):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    previous = calendar.previous(day)
    rows = con.execute("""SELECT s.*,d.close,b.close prev_close FROM stock s
        JOIN day d ON d.code=s.code AND d.date=?
        JOIN day b ON b.code=s.code AND b.date=? ORDER BY s.code""", (day, previous)).fetchall()
    pool = []
    for r in rows:
        if "ST" in r["name"].upper() or "退" in r["name"]:
            continue
        if r["prev_close"] <= 0 or abs(r["close"] - limit_price(r["prev_close"], r["limit_pct"])) > 0.005:
            continue
        hist = con.execute("SELECT high,low FROM day WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
                           (r["code"], day, cfg["lookback"])).fetchall()
        if len(hist) != cfg["lookback"] or any(x[0] <= 0 or x[1] <= 0 for x in hist):
            continue
        high, low = max(x[0] for x in hist), min(x[1] for x in hist)
        if high <= low:
            continue
        pos = (r["close"] - low) / (high - low)
        pool.append({"code": r["code"], "name": r["name"], "mkt": r["mkt"],
                     "limit_pct": r["limit_pct"], "prev_close": r["close"], "pos": pos})
    con.close()
    pool.sort(key=lambda p: (p["pos"], p["code"]))
    return {"date": day, "pool": pool, "hits": [p for p in pool if p["pos"] < cfg["position_threshold"]]}
