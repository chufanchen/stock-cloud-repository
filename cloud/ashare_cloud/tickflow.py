"""TickFlow REST adapter. No auction amounts are inferred from cumulative quotes.

API schema: https://docs.tickflow.org/zh-Hans/api-reference/openapi.json
A-share volume is lots: /zh-Hans/sdk/python-examples (daily turnover example).
"""
from __future__ import annotations

import math
import os
import re
import threading
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit

import requests

from .common import SHANGHAI
from .history import parse_rows, retry_after


class SourceError(RuntimeError):
    def __init__(self, reason, cooldown=0, permanent=False, diagnostic=None):
        self.reason, self.cooldown, self.permanent = reason, cooldown, permanent
        self.diagnostic = diagnostic or {}
        super().__init__(reason)


def http_diagnostic(response, path):
    """Classify error text locally, emitting ONLY fixed tags, never raw text/code.

    A JSON payload may echo a key or URL. A regex 'sanitizer' or truncation is
    insufficient, so even provider error codes are not copied to public logs.
    """
    body = getattr(response, "text", "")[:16384]
    content_type = response.headers.get("Content-Type", "").lower()
    values = []
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("not an error object")
        shape = "json"
        for key in ("code", "message", "error", "detail", "details"):
            value = payload.get(key)
            if isinstance(value, str):
                values.append(value[:4096])
            elif isinstance(value, dict):
                values.extend(v[:4096] for k, v in value.items()
                              if k in {"code", "message", "detail"} and isinstance(v, str))
    except (ValueError, TypeError):
        shape = "html" if "text/html" in content_type or "<html" in body.lower() or "<!doctype html" in body.lower() else "text"
        values = [body]
    text = " ".join(values).lower()
    category = "unspecified_denial"
    # Hints are not proof of the root cause. Missing patterns remain unspecified.
    patterns = (
        ("edge_challenge", ("cf-chl-", "cf-browser-verification", "challenge-platform", "cloudflare ray id")),
        ("ip_restriction", ("ip whitelist", "ip allowlist", "ip not allowed", "ip address is not allowed", "ip白名单", "ip 白名单")),
        ("credential_expired", ("key expired", "key has expired", "token expired", "密钥已过期")),
        ("credential_invalid", ("invalid api key", "invalid_api_key", "api key is invalid", "invalid token", "无效的api key", "无效的 api key")),
        ("subscription_inactive", ("subscription expired", "subscription is inactive", "subscription required", "no active subscription", "订阅已过期", "未开通", "未订阅")),
        ("quota_or_balance", ("insufficient balance", "insufficient credits", "quota exceeded", "余额不足", "额度不足")),
        ("permission_denied", ("permission denied", "no permission", "insufficient permission", "access denied", "无权限", "没有权限", "权限不足")),
    )
    for name, needles in patterns:
        if any(needle in text for needle in needles):
            category = name
            break
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        category = "edge_challenge"
    return {"http_status": int(response.status_code),
            "endpoint": path if path in {"/v1/klines/batch", "/v1/quotes"} else "other",
            "response_format": shape, "response_hint": category}


def api_symbol(symbol):
    if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
        raise ValueError("invalid_symbol")
    return symbol[2:] + "." + symbol[:2].upper()


def timestamp(value):
    # Reject seconds, strings, NaN, and boolean timestamps instead of guessing units.
    if type(value) is not int or not 946684800000 <= value < 4102444800000:
        raise ValueError("invalid_timestamp")
    return datetime.fromtimestamp(value / 1000, SHANGHAI)


def check_volume(volume, amount, low, high, symbol):
    if not all(type(x) in (int, float) and math.isfinite(x) for x in (volume, amount, low, high)):
        raise ValueError("invalid_volume")
    if volume < 0 or amount < 0 or (volume == 0) != (amount == 0):
        raise ValueError("invalid_volume")
    # Index turnover is not a trade in the index itself; no index VWAP check.
    if volume and symbol != "sh000001":
        vwap = amount / (volume * 100)
        if not low * 0.98 - 0.01 <= vwap <= high * 1.02 + 0.01:
            raise ValueError("volume_unit_mismatch")


def daily_rows(data, symbol):
    try:
        fields = ("timestamp", "open", "close", "high", "low", "volume", "amount")
        arrays = [data[k] for k in fields]
        if any(not isinstance(a, list) for a in arrays) or not arrays[0] or len({len(a) for a in arrays}) != 1:
            raise ValueError("invalid_columns")
        raw = []
        for stamp, opening, close, high, low, volume, amount in zip(*arrays):
            check_volume(volume, amount, low, high, symbol)
            raw.append([timestamp(stamp).date().isoformat(), opening, close, high, low, volume])
        rows = parse_rows({"data": {symbol: {"day": raw}}}, symbol)
        return sorted(rows, key=lambda r: r[1])
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("invalid_columns") from None


def quote_row(row, symbol):
    try:
        if row["symbol"] != api_symbol(symbol) or row["region"] != "CN":
            raise ValueError("symbol_mismatch")
        stamp = timestamp(row["timestamp"])
        check_volume(row["volume"], row["amount"], row["low"], row["high"], symbol)
        ext = row.get("ext") or {}
        name = ext.get("name") or ("上证指数" if symbol == "sh000001" else None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("missing_name")
        return {"date": stamp.date().isoformat(), "timestamp": stamp.strftime("%Y%m%d%H%M%S"),
                "name": name, "open": row["open"], "close": row["last_price"],
                "high": row["high"], "low": row["low"], "prev_close": row["prev_close"],
                "vol": row["volume"], "source": "tickflow_quote"}
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("invalid_quote") from None


class TickFlow:
    source = "tickflow"

    def __init__(self, pacer, get=None):
        self.pacer, self.get = pacer, get or requests.get
        self.key = os.getenv("TICKFLOW_API_KEY", "").strip()
        self.base = os.getenv("TICKFLOW_BASE_URL", "").strip().rstrip("/") or "https://api.tickflow.org"
        u = urlsplit(self.base)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment or u.path:
            raise ValueError("TICKFLOW_BASE_URL 必须为不含路径、账号或查询参数的 HTTPS 服务地址")
        self.metrics = {"requests": 0, "successes": 0, "errors": Counter(), "endpoints": Counter()}
        self.disabled = "missing_key" if not self.key else None
        self.blocked_until = 0
        self.failures = 0
        self.lock = threading.RLock()
        self.groups, self.cache = {}, {}
        self.last_failure = None

    def statistics(self):
        with self.lock:
            return {"source": self.source, "requests": self.metrics["requests"],
                    "attempt_errors": sum(self.metrics["errors"].values()), "recovered_symbols": 0,
                    "errors": dict(self.metrics["errors"]), "disabled": self.disabled,
                    "requests_by_endpoint": dict(self.metrics["endpoints"]),
                    "last_failure": self.last_failure}

    def request(self, path, params):
        # Serialized requests make the breaker and Retry-After global to this source.
        with self.lock:
            if self.disabled:
                raise SourceError(self.disabled, permanent=True)
            if self.pacer.clock() < self.blocked_until:
                raise SourceError("source_cooldown")
            self.pacer.wait()
            self.metrics["requests"] += 1
            self.metrics["endpoints"][path] += 1
            try:
                with self.get(self.base + path, params=params, headers={"x-api-key": self.key},
                              timeout=(5, 20), allow_redirects=False) as response:
                    status = response.status_code
                    if status != 200:
                        raise SourceError(f"http_{status}", retry_after(response.headers.get("Retry-After")),
                                          permanent=status in (401, 403), diagnostic=http_diagnostic(response, path))
                    data = response.json()
                    if not isinstance(data, dict) or "data" not in data:
                        raise SourceError("invalid_payload")
            except requests.Timeout:
                error = SourceError("timeout")
            except requests.RequestException:
                error = SourceError("connection_error")
            except ValueError:
                error = SourceError("invalid_json")
            except SourceError as exc:
                error = exc
            else:
                self.failures = 0
                self.metrics["successes"] += 1
                return data["data"]
            self.metrics["errors"][error.reason] += 1
            self.last_failure = error.diagnostic or {"endpoint": path, "response_hint": error.reason}
            self.failures += 1
            if error.permanent:
                self.disabled = error.reason
            elif self.failures >= 3:
                self.disabled = "circuit_open"
            self.blocked_until = self.pacer.clock() + max(error.cooldown, 60 if error.reason == "http_429" else 5)
            raise error from None

    def prepare(self, jobs, batch_size=50):
        self.groups, self.cache = {}, {}
        for offset in range(0, len(jobs), batch_size):
            group = jobs[offset:offset + batch_size]
            for symbol, _ in group:
                self.groups[symbol] = group

    def history(self, symbol, count):
        # At most the active batch(es) are cached; consumers remove their entries.
        with self.lock:
            if symbol not in self.cache:
                group = self.groups.get(symbol, [(symbol, count)])
                try:
                    data = self.request("/v1/klines/batch", {
                        "symbols": ",".join(api_symbol(s) for s, _ in group), "period": "1d",
                        "adjust": "none", "count": max(n for _, n in group)})
                    if not isinstance(data, dict):
                        raise SourceError("invalid_payload")
                    for s, _ in group:
                        try:
                            self.cache[s] = daily_rows(data.get(api_symbol(s), {}), s)
                        except ValueError:
                            self.cache[s] = SourceError("invalid_or_missing_daily_rows")
                except SourceError as exc:
                    for s, _ in group:
                        self.cache[s] = exc
            result = self.cache.pop(symbol)
            if isinstance(result, SourceError):
                raise result
            return result

    def quotes(self, symbols):
        data = self.request("/v1/quotes", {"symbols": ",".join(api_symbol(s) for s in symbols)})
        if not isinstance(data, list):
            raise SourceError("invalid_payload")
        mapping = {api_symbol(s): s for s in symbols}
        result, duplicates = {}, set()
        for row in data:
            if not isinstance(row, dict) or row.get("symbol") not in mapping:
                continue
            symbol = mapping[row["symbol"]]
            if symbol in result:
                duplicates.add(symbol)
                continue
            try:
                result[symbol] = quote_row(row, symbol)
            except ValueError:
                continue
        for symbol in duplicates:
            result.pop(symbol, None)
        return result
