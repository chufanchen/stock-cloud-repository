from __future__ import annotations

import math
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date
from pathlib import Path

import requests

from .common import now, read_json, write_json
from .engine import limit_price, validate_bar
from .history import history_client, HistoryFetchError, HistoryPaused

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
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.history_client = history_client(cfg)
        self.quote_diagnostics = {}

    def quotes(self, symbols):
        if self.cfg.get("quote_sources"):
            from .sources import priority_quotes
            # A 429/403 from quotes also applies to this account's history API.
            tickflow = getattr(self.history_client, "clients", {}).get("tickflow")
            result, self.quote_diagnostics = priority_quotes(symbols, self.cfg, self._quotes, tickflow=tickflow)
            return result
        return self._quotes(symbols)

    def _quotes(self, symbols):
        out = {}
        for start in range(0, len(symbols), 40):
            batch = symbols[start:start + 40]
            for attempt in range(3):
                try:
                    r = requests.get("https://qt.gtimg.cn/q=" + ",".join(batch), headers=HEADERS, timeout=20)
                    if getattr(r, "status_code", 200) in (401, 403):
                        from .tickflow import SourceError
                        raise SourceError("access_denied", permanent=True)
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
            if not self.cfg.get("quote_sources") and start % 400 == 0:
                print(f"行情快照：{min(start + 40, len(symbols))}/{len(symbols)}", flush=True)
            time.sleep(0.12)
        return out

    def history(self, symbol, count):
        return self.history_client.history(symbol, count)


def backfill(root, con, stocks, bars, day, previous, cfg, provider, partial, checkpoint=None):
    """Save only market progress; the caller cannot settle until this returns complete."""
    # Today's quote may already exist after a failed attempt. It cannot prove that
    # the preceding sessions were fetched, so exclude it when detecting gaps.
    latest = dict(con.execute("SELECT code,MAX(date) FROM day WHERE date<? GROUP BY code", (day,)))
    pending_file = Path(root) / "history-pending.json"
    carried = read_json(pending_file).get("since", {}) if pending_file.exists() else {}
    # Preserve the original gap across trading days too: a newer quote is not a
    # replacement for historical rows that a previous attempt failed to fetch.
    for code, cutoff in carried.items():
        latest[code] = min(latest.get(code, ""), cutoff or "")
    stale = [s for s in stocks if s["code"] in bars and latest.get(s["code"], "") < previous]
    started = last_checkpoint = time.monotonic()
    failures, completed, succeeded_codes = [], set(), set()
    successful = consecutive_failures = since_checkpoint = 0
    stop_reason = None
    client = getattr(provider, "history_client", None)
    budget = cfg.get("history_budget_seconds", 2400)
    print(f"历史补齐：待抓取 {len(stale)}/{len(stocks)}，并发 {cfg['fetch_workers']}，"
          f"全局最小间隔 {cfg.get('history_min_interval', 0.35)} 秒", flush=True)

    def pause(reason):
        nonlocal stop_reason
        stop_reason = stop_reason or reason
        if client:
            client.pacer.stop(stop_reason)

    def save(complete=False):
        nonlocal last_checkpoint, since_checkpoint
        # Ensure the raw close always overrides historical endpoint intraday bars.
        for code, b in bars.items():
            con.execute("INSERT OR REPLACE INTO day VALUES(?,?,?,?,?,?,?)",
                        (code, day, b["open"], b["close"], b["high"], b["low"], b["vol"]))
            mkt, lp = board_limit(code, b["name"])
            con.execute("""UPDATE stock SET name=?,mkt=?,limit_pct=? WHERE code=?
                        AND NOT EXISTS (SELECT 1 FROM day WHERE code=? AND date>?)""",
                        (b["name"], mkt, lp, code, code, day))
        con.commit()
        eligible = con.execute("SELECT COUNT(*) FROM day WHERE date=?", (previous,)).fetchone()[0]
        pending = [s["code"] for s in stale if s["code"] not in completed]
        remaining = dict(carried)
        for stock in stale:
            code = stock["code"]
            if code in succeeded_codes:
                remaining.pop(code, None)
            else:
                remaining[code] = latest.get(code) or None
        write_json(pending_file, {"updated_at": now().isoformat(), "since": remaining})
        previous_codes = {row[0] for row in con.execute("SELECT code FROM day WHERE date=?", (previous,))}
        ready = len(previous_codes - remaining.keys())
        diagnostic = {"date": day, "previous_date": previous, "status": "complete" if complete else "partial",
                      "updated_at": now().isoformat(), "requested": len(stale), "processed": len(completed),
                      "successful": successful, "failed": len(failures), "pending": pending,
                      "previous_rows": eligible, "universe": len(stocks),
                      "previous_coverage": eligible / len(stocks), "required_coverage": cfg["minimum_market_coverage"],
                      "history_ready_rows": ready, "history_coverage": ready / len(stocks),
                      "remaining_gaps": len(remaining),
                      "failure_reasons": dict(Counter(f["reason"] for f in failures)), "failures": failures,
                      "stop_reason": stop_reason, "elapsed_seconds": round(time.monotonic() - started, 1)}
        if client:
            diagnostic["network"] = client.statistics()
        partial.update(complete=complete, history_errors=[f["code"] for f in failures])
        write_json(Path(root) / "snapshots" / f"{day}.json", partial)
        write_json(Path(root) / "diagnostics" / f"history-{day}.json", diagnostic)
        # Complete state is published by execute() before settlement. Intermediate
        # saves use the same R2 CAS, retaining the original account and frozen plans.
        if checkpoint and not complete:
            checkpoint()
            print(f"行情进度已保存到 R2：本次成功 {successful}，失败 {len(failures)}，待处理 {len(pending)}；"
                  f"完整历史可用 {ready}/{len(stocks)} ({ready / len(stocks):.2%})，剩余缺口 {len(remaining)}", flush=True)
        last_checkpoint, since_checkpoint = time.monotonic(), 0
        return diagnostic

    counts = {}
    for stock in stale:
        last = latest.get(stock["code"])
        request_day = max(date.fromisoformat(day), now().date())
        gap = (request_day - date.fromisoformat(last)).days if last else 700
        counts[stock["mkt"] + stock["code"]] = min(700, max(10, gap + 5))
    if client and hasattr(client, "prepare"):
        client.prepare(list(counts.items()), bars, day, previous)
    iterator = iter(stale)
    pool = ThreadPoolExecutor(max_workers=cfg["fetch_workers"])
    tasks = {}

    def submit():
        if stop_reason:
            return
        stock = next(iterator, None)
        if stock is not None:
            symbol = stock["mkt"] + stock["code"]
            task = pool.submit(provider.history, symbol, counts[symbol])
            tasks[task] = stock

    try:
        for _ in range(cfg["fetch_workers"]):
            submit()
        while tasks:
            if time.monotonic() - started >= budget:
                pause("time_budget")
            ready, _ = wait(tasks, timeout=1, return_when=FIRST_COMPLETED)
            for task in ready:
                stock = tasks.pop(task)
                code = stock["code"]
                try:
                    rows = [x for x in task.result() if x[1] <= day]
                    if not any(x[1] <= previous for x in rows):
                        raise HistoryFetchError(stock["mkt"] + code, [{"attempt": 1, "reason": "no_historical_rows"}])
                    if client and client.verify_archive:
                        closing = next((x for x in rows if x[1] == day), None)
                        quote = bars[code]
                        if closing is None or any(x[0] != code for x in rows) or any(
                                abs(closing[i + 2] - quote[key]) > 0.011
                                for i, key in enumerate(("open", "close", "high", "low"))):
                            raise HistoryFetchError(stock["mkt"] + code,
                                [{"attempt": 1, "reason": "archive_price_mismatch"}])
                    # Filling an independent source cannot rewrite existing history.
                    operation = "IGNORE" if client and client.verify_archive else "REPLACE"
                    con.executemany(f"INSERT OR {operation} INTO day VALUES(?,?,?,?,?,?,?)", rows)
                    if not any(x[1] == previous for x in rows):
                        raise HistoryFetchError(stock["mkt"] + code, [{"attempt": 1, "reason": "missing_previous_session"}])
                    successful += 1
                    succeeded_codes.add(code)
                    consecutive_failures = 0
                    completed.add(code)
                except HistoryPaused as exc:
                    pause(exc.reason)
                except HistoryFetchError as exc:
                    failures.append({"code": code, "reason": exc.reason, "attempts": exc.failures})
                    completed.add(code)
                    consecutive_failures += 1
                except RuntimeError:
                    failures.append({"code": code, "reason": "history_error", "attempts": []})
                    completed.add(code)
                    consecutive_failures += 1
                since_checkpoint += 1
                if consecutive_failures >= cfg.get("history_failure_limit", 8):
                    pause("consecutive_failures")
                if len(completed) and len(completed) % 100 == 0:
                    print(f"补齐缺失日线：已处理 {len(completed)}/{len(stale)}，成功 {successful}，失败 {len(failures)}", flush=True)
                    if client:
                        stats = client.statistics()
                        print(f"历史网络统计：请求 {stats['requests']}，请求异常 {stats['attempt_errors']}，"
                              f"重试恢复 {stats['recovered_symbols']} 只", flush=True)
            if since_checkpoint >= cfg.get("history_checkpoint_every", 500) or time.monotonic() - last_checkpoint >= 180:
                save()
            while not stop_reason and len(tasks) < cfg["fetch_workers"]:
                old_count = len(tasks)
                submit()
                if len(tasks) == old_count:
                    break
        previous_codes = {row[0] for row in con.execute("SELECT code FROM day WHERE date=?", (previous,))}
        unresolved = (set(carried) | {s["code"] for s in stale}) - succeeded_codes
        ready = len(previous_codes - unresolved)
        all_processed = len(completed) == len(stale)
        good = (all_processed and ready / len(stocks) >= cfg["minimum_market_coverage"]
                and len(failures) / len(stocks) <= 1 - cfg["minimum_market_coverage"])
        diagnostic = save(complete=good)
        if not good:
            reason = {"time_budget": "达到本次抓取时间预算", "consecutive_failures": "连续取数失败，已停止请求",
                      "provider_access_denied": "行情源拒绝访问（WAF/403），已停止该来源全部请求"}.get(stop_reason, "完整性门槛未通过")
            raise ValueError(f"历史日线补齐不足：{reason}；上一交易日有报价 {len(previous_codes)}/{len(stocks)} "
                             f"({diagnostic['previous_coverage']:.2%})，完整历史可用 {ready}/{len(stocks)} "
                             f"({diagnostic['history_coverage']:.2%}，要求 {cfg['minimum_market_coverage']:.0%})；"
                             f"本次失败 {len(failures)}，待处理 {len(diagnostic['pending'])}，总剩余缺口 {len(unresolved)}。"
                             f"补齐进度已保存，账本未结算；详见 diagnostics/history-{day}.json")
        return partial
    finally:
        # Bound the submission queue to workers, so a failure never leaves thousands
        # of queued requests running while checkpoint/storage errors propagate.
        if client:
            client.pacer.stop("finished")
        pool.shutdown(wait=True, cancel_futures=True)


def tushare_auction_amounts(day):
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


def auction_amounts(day, root=None, cfg=None, bars=None):
    """Optional sources: a failed/missing paid API cannot block snapshot fallback."""
    from .auction import validate_capture
    cfg = cfg or {}
    order = cfg.get("auction_sources", ["tushare", "tencent_snapshot"])
    rows, notes = {}, []
    for source in order:
        if source == "tushare":
            try:
                batch, note = tushare_auction_amounts(day)
                notes.append(note)
            except (RuntimeError, requests.RequestException, ValueError, KeyError, TypeError):
                batch = {}
                notes.append("Tushare竞价接口不可用，尝试下一来源")
        else:
            batch = {}
            path = Path(root) / "auctions" / f"{day}.json" if root else None
            if path and path.exists():
                try:
                    plan = read_json(Path(root) / "account.json")["plans"][day]
                    batch, rejected = validate_capture(read_json(path), plan, day)
                    notes.append(f"腾讯早盘双样本：{len(batch)}只通过，{len(rejected)}只拒绝；量额推定并保守取小")
                except (ValueError, KeyError, TypeError, OSError):
                    notes.append("腾讯早盘证据不合格或与冻结计划不符")
            else:
                notes.append("没有已归档的腾讯早盘竞价证据")
        for code, value in batch.items():
            if code in rows:
                continue
            if bars is not None and (code not in bars or abs(value["price"] - bars[code]["open"]) > 0.011):
                notes.append(f"{source}成交价与收盘归档开盘价不符，拒绝该项")
                continue
            rows[code] = value
    notes.append(f"可用竞价证据{len(rows)}只；无证据的候选无法判定R0，不模拟买入")
    return rows, "；".join(dict.fromkeys(notes))


def collect(root, day, cfg, calendar, provider=None, checkpoint=None, archive_only=False):
    root = Path(root)
    cache = root / "snapshots" / f"{day}.json"
    saved = read_json(cache) if cache.exists() else None
    if saved:
        if not archive_only and saved.get("complete") and saved["date"] == day:
            return saved
    if archive_only:
        if not saved or saved.get("date") != day or day > now().date().isoformat():
            raise ValueError("历史补齐需要已经保存的该日收盘快照，不能用今天的报价替代")
    elif day != now().date().isoformat() or now().hour < 15:
        raise ValueError("实时接口仅用于当天收盘后；历史补跑必须有该日归档快照")
    provider = provider or Tencent(cfg)
    con = sqlite3.connect(root / "market.db")
    con.row_factory = sqlite3.Row
    stocks = [dict(r) for r in con.execute("SELECT * FROM stock ORDER BY code")]
    if len(stocks) < cfg["minimum_universe_size"]:
        raise ValueError("股票池规模不足")
    previous = calendar.previous(day)
    if archive_only:
        quotes = {s["mkt"] + s["code"]: saved.get("bars", {}).get(s["code"]) for s in stocks}
        quotes["sh000001"] = saved.get("index")
    else:
        quotes = provider.quotes([s["mkt"] + s["code"] for s in stocks] + ["sh000001"])
        if getattr(provider, "quote_diagnostics", None):
            write_json(root / "diagnostics" / f"quotes-{day}.json", provider.quote_diagnostics)
    bars, rejected = {}, []
    for s in stocks:
        b = quotes.get(s["mkt"] + s["code"])
        try:
            validate_bar(b, day)
            if b["timestamp"][:8] != day.replace("-", "") or b["timestamp"][8:12] < "1500":
                raise ValueError("尚未收盘")
            bars[s["code"]] = b
        except ValueError:
            rejected.append(s["code"])
    coverage = len(bars) / len(stocks)
    if coverage < cfg["minimum_market_coverage"]:
        raise ValueError(f"当日行情覆盖率 {coverage:.2%} 不足；可能停更或接口失败")
    index = quotes.get("sh000001")
    validate_bar(index, day)
    if index["timestamp"][:8] != day.replace("-", "") or index["timestamp"][8:12] < "1500":
        raise ValueError("上证指数尚未收盘")
    auctions, auction_note = ({}, saved.get("auction_note", "使用已归档竞价证据")) if archive_only else auction_amounts(day, root, cfg, bars)
    for code, a in auctions.items():
        if code in bars and abs(a["price"] - bars[code]["open"]) <= 0.011:
            bars[code]["auction_amount"] = a["amount"]
            bars[code]["auction_source"] = a["source"]
            bars[code]["auction_inferred"] = a.get("inferred", False)
    # Persist raw observations before historical requests, so interruptions retain evidence locally.
    partial = {"date": day, "captured_at": saved["captured_at"] if archive_only else now().isoformat(), "bars": bars, "index": index,
               "coverage": coverage, "missing_codes": rejected, "auction_note": auction_note,
               "complete": False}
    write_json(cache, partial)
    try:
        return backfill(root, con, stocks, bars, day, previous, cfg, provider, partial, checkpoint)
    finally:
        con.close()


def scan_database(db, day, cfg, calendar):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    previous = calendar.previous(day)
    rows = con.execute("""SELECT s.*,d.close,b.close prev_close FROM stock s
        JOIN day d ON d.code=s.code AND d.date=?
        JOIN day b ON b.code=s.code AND b.date=? ORDER BY s.code""", (day, previous)).fetchall()
    pool = []
    pending_file = Path(db).parent / "history-pending.json"
    unresolved = set(read_json(pending_file).get("since", {})) if pending_file.exists() else set()
    for r in rows:
        if r["code"] in unresolved:
            continue
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
