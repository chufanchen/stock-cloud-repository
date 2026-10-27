"""Bounded, globally paced historical requests; diagnostics never contain response bodies."""
from __future__ import annotations

import math
import random
import threading
import time
from collections import Counter
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

    def wait(self, not_before=0):
        while True:
            with self.lock:
                current = self.clock()
                if self.stop_reason or current >= self.deadline:
                    raise HistoryPaused(self.stop_reason or "time_budget")
                delay = max(self.next_start, self.blocked_until, not_before) - current
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
        self.lock = threading.Lock()
        self.preferred = URLS[0]
        self.health = {url: {"blocked_until": 0, "failures": 0} for url in URLS}
        self.metrics = {url: {"requests": 0, "successes": 0, "errors": Counter(), "seconds": 0.0} for url in URLS}
        self.recovered = 0

    def statistics(self):
        with self.lock:
            hosts = {url.split('/')[2]: {**m, "errors": dict(m["errors"]), "seconds": round(m["seconds"], 2)}
                     for url, m in self.metrics.items()}
            return {"requests": sum(m["requests"] for m in hosts.values()),
                    "attempt_errors": sum(sum(m["errors"].values()) for m in hosts.values()),
                    "recovered_symbols": self.recovered, "hosts": hosts}

    def _endpoint(self, previous=None):
        with self.lock:
            current = self.pacer.clock()
            ready = [url for url in URLS if self.health[url]["blocked_until"] <= current]
            if ready:
                candidates = [url for url in ready if url != previous] or ready
                url = self.preferred if self.preferred in candidates else candidates[0]
            else:
                url = min(URLS, key=lambda u: self.health[u]["blocked_until"])
            return url, self.health[url]["blocked_until"]

    def _record(self, url, elapsed, reason=None, after=0, recovered=False):
        with self.lock:
            metric, health = self.metrics[url], self.health[url]
            metric["requests"] += 1
            metric["seconds"] += elapsed
            if reason is None:
                metric["successes"] += 1
                self.recovered += int(recovered)
                health.update(blocked_until=0, failures=0)
                self.preferred = url
                return
            metric["errors"][reason] += 1
            # A bad stock response does not prove the whole host is unavailable.
            if reason.startswith("http_") or reason in {"timeout", "connection_error", "request_error", "invalid_json"}:
                health["failures"] += 1
                # A repeated 501 previously held both workers for five minutes
                # per attempt. Keep transient host backoff bounded; explicit
                # Retry-After and provider-wide rate-limit waits still win.
                health["blocked_until"] = self.pacer.clock() + max(after, min(30, 5 * 2 ** min(health["failures"] - 1, 3)))
                if self.preferred == url:
                    self.preferred = next(other for other in URLS if other != url)
            errors = sum(sum(m["errors"].values()) for m in self.metrics.values())
        if errors <= 5 or errors % 25 == 0:
            print(f"历史接口重试：{url.split('/')[2]} {reason}，累计请求异常 {errors}（含后来重试成功的请求）", flush=True)

    def _get(self, *args, **kwargs):
        if self.get:
            return self.get(*args, **kwargs)
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
        return self.local.session.get(*args, **kwargs)

    def history(self, symbol, count):
        failures = []
        previous, retry_at = None, 0
        for attempt in range(self.attempts):
            url, available = self._endpoint(previous)
            self.pacer.wait(max(available, retry_at))
            started = self.pacer.clock()
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
                        self._record(url, self.pacer.clock() - started, recovered=bool(failures))
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
            if reason in {"http_501", "connection_error", "invalid_json"} and hasattr(self.local, "session"):
                # Do not keep reusing a connection that produced a proxy/protocol error.
                self.local.session.close()
                del self.local.session
            self._record(url, self.pacer.clock() - started, reason, after)
            delay = min(120, (15 if reason in {"http_403", "http_429"} else 2) * 2 ** attempt)
            if reason in {"http_403", "http_429", "http_503"} or after:
                # Rate-limit waits remain provider-wide, even when switching hosts.
                self.pacer.failed(max(after, delay + random.uniform(0, 1)))
            retry_at = self.pacer.clock() + (delay if not reason.startswith("http_") else 0)
            previous = url
        raise HistoryFetchError(symbol, failures)
