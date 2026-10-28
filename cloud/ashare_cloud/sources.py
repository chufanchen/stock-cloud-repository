"""Priority by data capability; independent providers share a bounded run budget."""
from __future__ import annotations

import math
import threading
from collections import Counter
from datetime import datetime

import requests

from .common import now
from .engine import validate_bar
from .history import (EastmoneyHistoryClient, HistoryClient, HistoryFetchError,
                      HistoryPaused, Pacer, SinaHistoryClient)
from .tickflow import SourceError, TickFlow


class ProviderPacer(Pacer):
    """A denied/cooling source cannot stop other sources or reset the total budget."""
    def __init__(self, parent):
        super().__init__(0, max(0, parent.deadline - parent.clock()), parent.clock, parent.sleep)
        self.parent = parent

    def wait(self, not_before=0):
        if self.stop_reason:
            raise HistoryPaused(self.stop_reason)
        self.parent.wait(max(not_before, self.blocked_until))
        if self.stop_reason:
            raise HistoryPaused(self.stop_reason)


class PriorityHistory:
    source = "priority"
    verify_archive = True

    def __init__(self, cfg, pacer=None, clients=None):
        self.pacer = pacer or Pacer(cfg.get("history_min_interval", 1), cfg.get("history_budget_seconds", 2400))
        self.order = cfg["history_sources"]
        self.batch_size = cfg.get("tickflow_batch_size", 50)
        factories = {"sina": SinaHistoryClient, "eastmoney": EastmoneyHistoryClient, "tencent": HistoryClient}
        # One request per source/stock. Rate waits and retries carry across stocks;
        # fallback should not wait through four attempts on a broken endpoint.
        self.clients = clients if clients is not None else {
            name: TickFlow(ProviderPacer(self.pacer)) if name == "tickflow" else
                  factories[name]({**cfg, "history_attempts": 1}, pacer=ProviderPacer(self.pacer))
            for name in self.order}
        self.lock = threading.RLock()
        self.source_locks = {name: threading.Lock() for name in self.order}
        self.disabled, self.streaks, self.reasons = {}, Counter(), Counter()
        self.selected, self.fallbacks = {}, {}
        self.context = None

    def prepare(self, jobs, bars, day, previous):
        self.context = (bars, day, previous)
        for client in self.clients.values():
            if hasattr(client, "prepare"):
                client.prepare(jobs, self.batch_size)

    def _validate(self, rows, symbol):
        if not self.context:
            return rows
        bars, day, previous = self.context
        rows = [r for r in rows if r[1] <= day]
        closing = next((r for r in rows if r[1] == day), None)
        if any(r[0] != symbol[2:] for r in rows):
            raise SourceError("symbol_mismatch")
        if not any(r[1] == previous for r in rows):
            raise SourceError("missing_previous_session")
        quote = bars[symbol[2:]]
        if closing is None or any(abs(closing[i + 2] - quote[key]) > 0.011
                                  for i, key in enumerate(("open", "close", "high", "low"))):
            raise SourceError("archive_price_mismatch")
        return rows

    def _available(self, name, client):
        if name in self.disabled:
            raise SourceError(self.disabled[name], permanent=True)
        if getattr(client, "disabled", None):
            raise SourceError(client.disabled, permanent=True)
        if getattr(client, "blocked_until", 0) > self.pacer.clock():
            raise SourceError("source_cooldown")
        # Skip cooling sources immediately; preserve their Retry-After for later.
        if getattr(client.pacer, "blocked_until", 0) > self.pacer.clock():
            raise SourceError("source_cooldown")
        health = getattr(client, "health", {})
        if health and all(h["blocked_until"] > self.pacer.clock() for h in health.values()):
            raise SourceError("source_cooldown")

    def history(self, symbol, count):
        failures = []
        for name in self.order:
            if self.pacer.stop_reason or self.pacer.clock() >= self.pacer.deadline:
                raise HistoryPaused(self.pacer.stop_reason or "time_budget")
            client = self.clients[name]
            with self.source_locks[name]:
                try:
                    self._available(name, client)
                    rows = self._validate(client.history(symbol, count), symbol)
                except HistoryPaused as exc:
                    if exc.reason != "provider_access_denied":
                        raise
                    error = SourceError("access_denied", permanent=True)
                except HistoryFetchError as exc:
                    error = SourceError(exc.reason, permanent=exc.reason in {"http_401", "http_403", "access_denied"})
                except SourceError as exc:
                    error = exc
                else:
                    with self.lock:
                        self.streaks[name] = 0
                        self.selected[symbol] = name
                        if failures:
                            self.fallbacks[symbol] = failures
                    return rows
                with self.lock:
                    self.reasons[f"{name}:{error.reason}"] += 1
                    if error.permanent:
                        self.disabled[name] = error.reason
                    elif name != "tickflow" and (error.reason.startswith("http_") or error.reason in
                            {"timeout", "connection_error", "request_error", "invalid_json"}):
                        self.streaks[name] += 1
                        if self.streaks[name] >= 3:
                            self.disabled[name] = "circuit_open"
                failures.append({"source": name, "attempt": 1, "reason": error.reason})
        raise HistoryFetchError(symbol, failures)

    def statistics(self):
        with self.lock:
            sources = {name: c.statistics() for name, c in self.clients.items()}
            return {"source": self.source, "priority": self.order,
                    "requests": sum(s["requests"] for s in sources.values()),
                    "attempt_errors": sum(s["attempt_errors"] for s in sources.values()),
                    "recovered_symbols": len(self.fallbacks), "sources": sources,
                    "disabled": dict(self.disabled), "failure_reasons": dict(self.reasons),
                    "selected_sources": dict(self.selected), "fallbacks": dict(self.fallbacks)}


def valid_close(bar, day):
    try:
        validate_bar(bar, day)
        stamp = bar["timestamp"]
        volume = bar["vol"]
        if not isinstance(stamp, str) or len(stamp) != 14:
            return False
        datetime.strptime(stamp, "%Y%m%d%H%M%S")
        return (isinstance(stamp, str) and len(stamp) == 14 and stamp.isdigit()
                and stamp[:8] == day.replace("-", "") and "150000" <= stamp[8:] <= "235959"
                and type(volume) in (int, float) and math.isfinite(volume) and volume >= 0
                and isinstance(bar["name"], str) and bool(bar["name"].strip()))
    except (ValueError, TypeError, KeyError):
        return False


def priority_quotes(symbols, cfg, legacy, day=None, tickflow=None, pacer=None):
    day = day or now().date().isoformat()
    pacer = pacer or Pacer(cfg.get("history_min_interval", 1), cfg.get("quote_budget_seconds", 600))
    order = cfg["quote_sources"]
    tickflow = tickflow or (TickFlow(pacer) if "tickflow" in order else None)
    result, selected, disabled, errors = {}, Counter(), {}, Counter()
    streaks = Counter()
    for offset in range(0, len(symbols), 40):
        missing = symbols[offset:offset + 40]
        for name in order:
            if not missing or name in disabled:
                continue
            if pacer.stop_reason or pacer.clock() >= pacer.deadline:
                disabled[name] = "time_budget"
                break
            try:
                if name == "tickflow":
                    batch = tickflow.quotes(missing)
                else:
                    pacer.wait()
                    batch = legacy(missing)
            except HistoryPaused:
                disabled[name] = "time_budget"
                break
            except SourceError as exc:
                errors[f"{name}:{exc.reason}"] += 1
                if exc.permanent:
                    disabled[name] = exc.reason
                elif exc.reason != "source_cooldown":
                    streaks[name] += 1
                batch = {}
            except (requests.RequestException, RuntimeError):
                errors[f"{name}:request_failed"] += 1
                streaks[name] += 1
                batch = {}
            else:
                streaks[name] = 0
            if streaks[name] >= 3:
                disabled[name] = "circuit_open"
            accepted = {s: batch[s] for s in missing if s in batch and valid_close(batch[s], day)}
            errors[f"{name}:missing_or_invalid_close"] += len(missing) - len(accepted)
            result.update(accepted)
            selected[name] += len(accepted)
            missing = [s for s in missing if s not in accepted]
        if offset % 400 == 0:
            print(f"行情快照：{min(offset + 40, len(symbols))}/{len(symbols)}，已验证 {len(result)}", flush=True)
    return result, {"priority": order, "selected_counts": dict(selected), "disabled": disabled,
                    "failure_reasons": dict(errors), "tickflow": tickflow.statistics() if tickflow else None}
