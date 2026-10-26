"""Bounded, globally paced historical requests; diagnostics never contain response bodies."""
from __future__ import annotations

import math
import random
import threading
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import requests

URLS = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get")
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}


class HistoryFetchError(RuntimeError):
    def __init__(self, symbol, failures):
        self.failures = failures
        self.reason = failures[-1]["reason"]
        super().__init__(f"历史日线取数失败：{symbol}（{self.reason}，{len(failures)} 次尝试）")


class HistoryPaused(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Pacer:
    """One gate for both hosts and all workers, including retries."""
    def __init__(self, interval=0.35, budget=2400, clock=None, sleep=None):
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.base = self.interval = interval
        self.deadline = self.clock() + budget
        self.next_start = self.blocked_until = 0.0
        self.lock = threading.Lock()
        self.stop_reason = None
        self.successes = 0

    def stop(self, reason):
        with self.lock:
            self.stop_reason = reason

    def wait(self):
        while True:
            with self.lock:
                current = self.clock()
                if self.stop_reason or current >= self.deadline:
                    raise HistoryPaused(self.stop_reason or "time_budget")
                delay = max(self.next_start, self.blocked_until) - current
                if delay <= 0:
                    self.next_start = current + self.interval
                    return
                delay = min(delay, self.deadline - current, 1.0)
            self.sleep(delay)

    def failed(self, delay):
        with self.lock:
            self.interval = min(2.0, self.interval * 1.5)
            self.blocked_until = max(self.blocked_until, self.clock() + delay)
            self.successes = 0

    def succeeded(self):
        with self.lock:
            self.successes += 1
            if self.successes >= 20:
                self.interval = max(self.base, self.interval * 0.8)
                self.successes = 0


def retry_after(value):
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds > 0 else 0
    except (TypeError, ValueError):
        try:
            stamp = parsedate_to_datetime(value)
            return max(0, (stamp - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def parse_rows(payload, symbol):
    try:
        if payload.get("code", 0) != 0:
            raise ValueError("provider_error")
        data = payload.get("data", {}).get(symbol, {})
        # Adjusted prices must never enter the limit-price strategy database.
        raw = data.get("day")
        if not raw:
            raise ValueError("adjusted_only" if data.get("qfqday") or data.get("hfqday") else "empty_data")
        rows = []
        seen = set()
        for x in raw:
            day = date.fromisoformat(x[0]).isoformat()
            opening, close, high, low, vol = map(float, x[1:6])
            if (day in seen or not all(math.isfinite(v) for v in (opening, close, high, low, vol))
                    or min(opening, close, high, low) <= 0 or vol < 0
                    or low > min(opening, close) or high < max(opening, close)):
                raise ValueError("invalid_rows")
            seen.add(day)
            rows.append((symbol[2:], day, opening, close, high, low, vol))
        return rows
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        # Only these fixed tags may reach logs; never forward provider text.
        reason = str(exc) if str(exc) in {"provider_error", "adjusted_only", "empty_data", "invalid_rows"} else "invalid_rows"
        raise ValueError(reason) from None


class HistoryClient:
    def __init__(self, cfg=None, pacer=None, get=None):
        cfg = cfg or {}
        self.pacer = pacer or Pacer(cfg.get("history_min_interval", 0.35), cfg.get("history_budget_seconds", 2400))
        self.attempts = cfg.get("history_attempts", 4)
        self.local = threading.local()
        self.get = get

    def _get(self, *args, **kwargs):
        if self.get:
            return self.get(*args, **kwargs)
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        return self.local.session.get(*args, **kwargs)

    def history(self, symbol, count):
        failures = []
        for attempt in range(self.attempts):
            self.pacer.wait()
            url = URLS[attempt % len(URLS)]
            after = 0
            try:
                with self._get(url, params={"param": f"{symbol},day,,,{count},"},
                               headers=HEADERS, timeout=(5, 15), allow_redirects=False) as response:
                    status = response.status_code
                    if status != 200:
                        after = retry_after(response.headers.get("Retry-After"))
                        reason = f"http_{status}"
                    else:
                        try:
                            payload = response.json()
                        except ValueError:
                            raise ValueError("invalid_json") from None
                        rows = parse_rows(payload, symbol)
                        self.pacer.succeeded()
                        return rows
            except requests.Timeout:
                reason = "timeout"
            except requests.ConnectionError:
                reason = "connection_error"
            except requests.RequestException:
                reason = "request_error"
            except ValueError as exc:
                reason = str(exc)
            failures.append({"attempt": attempt + 1, "host": url.split("/")[2], "reason": reason})
            delay = min(120, (15 if reason in {"http_403", "http_429"} else 2) * 2 ** attempt)
            self.pacer.failed(max(after, delay + random.uniform(0, 1)))
        raise HistoryFetchError(symbol, failures)
