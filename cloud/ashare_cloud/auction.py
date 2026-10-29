"""Keyless Tencent opening snapshots; raw evidence is separate from the R2 ledger.

Inspired by the original watch_auction.py. Quotes before the opening match or
after continuous trading starts are never used as executed auction turnover.
This is a validated snapshot inference, not an exchange auction feed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import tarfile
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import requests

from .common import Calendar, SHANGHAI, now, read_json, write_json
from .engine import verify_plan

SOURCE = "tencent_opening_snapshot_v1"
START, END = "092505", "092950"
MAX_BYTES = 2 * 1024 * 1024
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


class CaptureError(RuntimeError):
    pass


def symbol(code):
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise ValueError("invalid_code")
    return ("bj" if code.startswith(("4", "8", "92")) else
            "sh" if code.startswith(("6", "9")) else "sz") + code


def plan_symbols(plan, day):
    verify_plan(plan)
    if plan["trade_date"] != day:
        raise ValueError("竞价采集必须使用当天的盘前冻结计划")
    symbols = [symbol(p["code"]) for p in plan["picks"]]
    if len(symbols) != len(set(symbols)) or len(symbols) > 200:
        raise ValueError("竞价候选重复或超过200只")
    return symbols


def parse_quotes(body, symbols):
    """Keep the upstream strings and timestamp. Never evaluate the response as JS."""
    out = {}
    for line in body.decode("gbk", "replace").split(";"):
        match = re.fullmatch(r'\s*v_((?:sh|sz|bj)\d{6})="([^"\r\n]*)"\s*', line)
        if not match or match[1] not in symbols:
            continue
        fields = match[2].split("~")
        if len(fields) <= 37 or any(len(f) > 512 for f in fields):
            continue
        # Include book fields for audit only; they never enter the R0 calculation.
        raw = {str(i): fields[i] for i in (1, 2, 3, 4, 5, 6, 9, 10, 19, 20, 30, 37)}
        if match[1] in out:
            raise CaptureError("duplicate_quote")
        out[match[1]] = raw
    return out


def fetch_quotes(symbols):
    if not symbols or len(symbols) > 40 or any(symbol(s[2:]) != s for s in symbols):
        raise ValueError("invalid_quote_batch")
    try:
        response = requests.get("https://qt.gtimg.cn/q=" + ",".join(symbols),
                                headers=HEADERS, timeout=(5, 10), allow_redirects=False)
    except requests.RequestException:
        raise CaptureError("network_error") from None
    if response.status_code != 200:
        raise CaptureError("http_" + str(response.status_code))
    return parse_quotes(response.content, symbols)


def sample_value(sample, sym, day):
    """Return a conservative amount only after timestamp + price + unit checks."""
    try:
        captured = datetime.fromisoformat(sample["captured_at"])
        if captured.utcoffset() is None:
            raise ValueError("missing_timezone")
        captured = captured.astimezone(SHANGHAI)
        raw = sample["raw"]
        if not isinstance(raw, dict) or any(not isinstance(raw.get(str(i)), str) or len(raw[str(i)]) > 512
                                           for i in (2, 3, 4, 5, 6, 30, 37)):
            raise ValueError("invalid_raw_fields")
        stamp = raw["30"]
        if not isinstance(stamp, str) or not re.fullmatch(r"\d{14}", stamp):
            raise ValueError("invalid_provider_time")
        source_time = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        if (captured.date().isoformat() != day or source_time.date().isoformat() != day
                or not START <= captured.strftime("%H%M%S") <= END
                or not "092500" <= stamp[8:] < "093000"
                or source_time > captured):
            raise ValueError("outside_auction_window")
        if raw["2"] != sym[2:]:
            raise ValueError("symbol_mismatch")
        price, opening, previous, lots, wan = (Decimal(raw[str(i)]) for i in (3, 5, 4, 6, 37))
        if any(not x.is_finite() or x <= 0 for x in (price, opening, previous, lots, wan)):
            raise ValueError("invalid_quote_numbers")
        if price != opening:
            raise ValueError("opening_price_mismatch")
        displayed, calculated = wan * 10000, lots * 100 * opening
        # Tencent's public quote has rounded turnover (10k yuan) and lot volume.
        # Allow at most 100 yuan / one lot rounding; never allow a percentage-only
        # tolerance which could conceal unit changes. Take the smaller estimate.
        if abs(displayed - calculated) > max(Decimal(100), opening * 100):
            raise ValueError("volume_amount_mismatch")
        amount = min(displayed, calculated).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if any(not math.isfinite(float(x)) for x in (amount, opening, previous)):
            raise ValueError("numeric_overflow")
        return {"amount": float(amount), "price": float(opening), "previous": float(previous),
                "source": SOURCE, "inferred": True, "captured_at": captured.isoformat(),
                "provider_timestamp": stamp, "lots": str(lots), "amount_wan": str(wan)}
    except (KeyError, TypeError, ArithmeticError):
        raise ValueError("invalid_sample") from None


def confirmed(samples, sym, day):
    if not isinstance(samples, list) or len(samples) != 2:
        raise ValueError("need_two_samples")
    first, last = [sample_value(s, sym, day) for s in samples]
    seconds = (datetime.fromisoformat(last["captured_at"]) - datetime.fromisoformat(first["captured_at"])).total_seconds()
    if not 2 <= seconds <= 30 or last["provider_timestamp"] < first["provider_timestamp"]:
        raise ValueError("invalid_confirmation_interval")
    if any(first[k] != last[k] for k in ("price", "previous", "lots", "amount_wan")):
        raise ValueError("unstable_auction_quote")
    return last


def validate_capture(record, plan, day):
    symbols = plan_symbols(plan, day)
    if (not isinstance(record, dict) or record.get("schema") != 1 or record.get("source") != SOURCE or record.get("date") != day
            or record.get("plan_hash") != plan["hash"]):
        raise ValueError("capture_plan_mismatch")
    entries = record.get("samples", {})
    if not isinstance(entries, dict) or set(entries) - set(symbols):
        raise ValueError("unexpected_capture_symbol")
    result, rejected = {}, {}
    picks = {p["code"]: p for p in plan["picks"]}
    for sym, samples in entries.items():
        try:
            value = confirmed(samples, sym, day)
            if abs(value["previous"] - picks[sym[2:]]["prev_close"]) > 0.011:
                raise ValueError("frozen_previous_close_mismatch")
            result[sym[2:]] = value
        except (ValueError, TypeError):
            rejected[sym[2:]] = "invalid_or_unstable_sample"
    return result, rejected


def capture(plan, day, output, interval=3, fetch=fetch_quotes, clock=now, sleep=time.sleep):
    if not 2 <= interval <= 10 or not math.isfinite(interval):
        raise ValueError("采样间隔必须为2到10秒")
    symbols = plan_symbols(plan, day)
    current = clock().astimezone(SHANGHAI)
    if current.date().isoformat() != day or not "092400" <= current.strftime("%H%M%S") <= END:
        raise ValueError("仅能在当日北京时间09:24至09:29:50启动，不能补采历史竞价")
    record = {"schema": 1, "source": SOURCE, "date": day, "plan_hash": plan["hash"],
              "samples": {}, "status": "running", "diagnostic_counts": {}}
    previous, errors, streak = {}, Counter(), 0
    if not symbols:
        record["status"] = "no_candidates"
        write_json(output, record)
        return record
    while True:
        current = clock().astimezone(SHANGHAI)
        if current.date().isoformat() != day or current.strftime("%H%M%S") > END:
            break
        if current.strftime("%H%M%S") < START:
            sleep(min(interval, 1))
            continue
        pending = [s for s in symbols if s not in record["samples"]]
        fatal = False
        for offset in range(0, len(pending), 40):
            if clock().astimezone(SHANGHAI).strftime("%H%M%S") > END:
                break
            batch = pending[offset:offset + 40]
            try:
                quotes = fetch(batch)
                streak = 0
            except CaptureError as exc:
                reason = str(exc) if re.fullmatch(r"http_\d{3}|network_error|duplicate_quote", str(exc)) else "request_failed"
                errors[reason] += 1
                streak += 1
                if reason in {"http_401", "http_403", "http_429"} or streak >= 3:
                    fatal = True
                    break
                continue
            # Timestamp every returned batch after the response, never at loop start.
            captured = clock().astimezone(SHANGHAI).isoformat()
            for sym in batch:
                sample = {"captured_at": captured, "raw": quotes.get(sym)}
                try:
                    sample_value(sample, sym, day)
                except (ValueError, TypeError):
                    errors["invalid_sample"] += 1
                    previous.pop(sym, None)
                    continue
                if sym in previous:
                    try:
                        confirmed([previous[sym], sample], sym, day)
                    except ValueError:
                        errors["awaiting_stable_confirmation"] += 1
                    else:
                        record["samples"][sym] = [previous[sym], sample]
                previous[sym] = sample
            record["diagnostic_counts"] = dict(errors)
            write_json(output, record)
            # Small batch delay also bounds multi-batch requests.
            if offset + 40 < len(pending):
                sleep(1)
        valid, _ = validate_capture(record, plan, day)
        # A stable quote with a different prior close is not a completed candidate.
        for sym in list(record["samples"]):
            if sym[2:] not in valid:
                record["samples"].pop(sym)
        print(f"竞价快照已验证：{len(valid)}/{len(symbols)}", flush=True)
        if fatal or len(valid) == len(symbols):
            break
        sleep(interval)
    valid, rejected = validate_capture(record, plan, day)
    record.update(status="complete" if len(valid) == len(symbols) else "partial",
                  diagnostic_counts=dict(errors), validated_count=len(valid),
                  missing=[s[2:] for s in symbols if s[2:] not in valid])
    write_json(output, record)
    return record


def capture_key(store, day, plan):
    plan_symbols(plan, day)
    return store.key(f"auction-captures/{day}/{plan['hash']}.json")


def publish_capture(store, record, plan, day):
    """First valid capture wins. Never update account HEAD or upload databases."""
    from botocore.exceptions import ClientError
    valid, _ = validate_capture(record, plan, day)
    if not valid:
        return False
    body = json.dumps(record, ensure_ascii=False, allow_nan=False).encode()
    if len(body) > MAX_BYTES:
        raise ValueError("竞价证据包过大")
    try:
        store.client.put_object(Bucket=store.bucket, Key=capture_key(store, day, plan),
                                Body=body, ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
            raise RuntimeError("该日同计划竞价证据已存在，禁止覆盖；收盘流程将校验已存证据") from None
        raise
    return True


def restore_capture(store, root, plan, day):
    from botocore.exceptions import ClientError
    target = Path(root) / "auctions" / f"{day}.json"
    if target.exists():
        return
    try:
        obj = store.client.get_object(Bucket=store.bucket, Key=capture_key(store, day, plan))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return
        raise
    body = obj["Body"].read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("竞价证据包过大")
    record = json.loads(body)
    validate_capture(record, plan, day)
    write_json(target, record)


def read_cloud_account(store):
    """Download only the small state bundle, with its hash, not the 120MB database."""
    manifest = store.head()
    if not manifest or not manifest["state_key"].startswith(store.key("states/")):
        raise ValueError("R2 尚未初始化或状态包位置不合格")
    body = store.client.get_object(Bucket=store.bucket, Key=manifest["state_key"])["Body"].read(64 * 1024 * 1024 + 1)
    if len(body) > 64 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != manifest["state_sha256"]:
        raise ValueError("R2 状态包校验失败")
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        members = [m for m in archive.getmembers() if m.name == "account.json"]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 16 * 1024 * 1024:
            raise ValueError("R2 账户文件不合格")
        return json.load(archive.extractfile(members[0]))


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="腾讯免Key竞价快照：早盘采集，晚间按冻结计划读取")
    parser.add_argument("--state-dir", type=Path, help="本地账户目录；指定后完全不读写R2")
    parser.add_argument("--output-dir", type=Path, default=Path(".server-runs/auction"))
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--probe", action="store_true", help="只检查三个报价样本；不存证、不读取账户")
    args = parser.parse_args(argv)
    if args.probe:
        quotes = fetch_quotes(["sh600000", "sz000001", "bj920002"])
        print(json.dumps({"probe_only": True, "quotes": {s: {"provider_timestamp": q["30"],
              "fields_present": all(q.get(str(i)) for i in (3, 4, 5, 6, 30, 37))} for s, q in quotes.items()},
              "note": "仅字段可达性检查；未验证早盘实际语义，不产生竞价证据"}, ensure_ascii=False))
        return 0 if len(quotes) == 3 and all(all(q.get(str(i)) for i in (3, 4, 5, 6, 30, 37))
                                             for q in quotes.values()) else 1
    day = now().date().isoformat()
    if not Calendar().is_session(day):
        print(f"{day} 休市，跳过竞价采集。")
        return 0
    if not "092400" <= now().strftime("%H%M%S") <= END:
        raise ValueError("请在北京时间09:24至09:29:50启动；错过窗口不能补采")
    store = None
    if args.state_dir:
        account = read_json(args.state_dir / "account.json")
    else:
        from .storage import Store
        store = Store()
        account = read_cloud_account(store)
    plan = account.get("plans", {}).get(day)
    if plan is None:
        raise ValueError("当天没有盘前冻结计划，禁止临时选股或补造计划")
    output = ((args.state_dir / "auctions") if args.state_dir else args.output_dir) / f"{day}.json"
    if output.exists():
        raise ValueError("本地当日采样文件已存在，请先核对并归档；不会覆盖")
    record = capture(plan, day, output, args.interval)
    if store and publish_capture(store, record, plan, day):
        print("竞价证据已保存到私有R2；账户、计划和HEAD未更新。")
    print("采集状态：" + record["status"])
    return 0 if record["status"] in {"complete", "no_candidates"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Do not expose vendor bodies, request URLs, headers or SDK error text.
        print("ERROR: " + (str(exc) if isinstance(exc, (ValueError, CaptureError)) else
                          "竞价采集/存储失败，请检查网络、权限或已有采集文件。"))
        raise SystemExit(1)
