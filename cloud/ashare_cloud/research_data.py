"""Freeze optional research evidence before a new meeting; no account or engine writes."""
from __future__ import annotations

import argparse
import copy
import json
import re
from datetime import date, time
from pathlib import Path

from .common import digest, now, read_json, write_json
from .research_sources import LABELS, PROVIDERS, FeedError, PublicClient, fetch

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "research_data.json"


def validate_config(cfg):
    cfg = copy.deepcopy(cfg)
    if type(cfg.get("enabled")) is not bool:
        raise ValueError("研究数据 enabled 必须为布尔值")
    for key, low, high in (("budget_seconds", 30, 600), ("max_requests", 10, 200),
                          ("max_symbols", 1, 30), ("max_context_chars", 4000, 40000)):
        if type(cfg.get(key)) is not int or not low <= cfg[key] <= high:
            raise ValueError("研究数据配置范围错误：" + key)
    if type(cfg.get("request_interval")) not in (int, float) or not 1 <= cfg["request_interval"] <= 10:
        raise ValueError("研究数据请求间隔需为1至10秒")
    sources = cfg.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(LABELS):
        raise ValueError("研究数据来源类别不完整")
    for kind, providers in sources.items():
        if not isinstance(providers, list) or not providers or len(set(providers)) != len(providers) or any(x not in (PROVIDERS[kind], "archive") for x in providers):
            raise ValueError("该研究类别不支持所选来源：" + kind)
    return cfg


def focus_symbols(state, day, max_symbols):
    groups = [state.get("positions", []), state.get("settlements", {}).get(day, {}).get("positions", [])]
    settlement = state.get("settlements", {}).get(day, {})
    groups.extend(settlement.get(k, []) for k in ("buys", "sells", "rejects"))
    groups.extend(p.get("picks", []) for p in state.get("plans", {}).values()
                  if p.get("data_date") == day or p.get("trade_date") == day)
    symbols = []
    for group in groups:
        for row in group:
            symbol = row.get("code")
            if isinstance(symbol, str) and re.fullmatch(r"\d{6}", symbol) and symbol not in symbols:
                symbols.append(symbol)
    return symbols[:max_symbols], len(symbols)


def missing(kind, reason):
    return {"label": LABELS[kind], "status": "missing", "reason": reason, "source": None,
            "rows": [], "row_count": None, "total": None, "complete": False}


def check_archive(value, day):
    unsigned = {k: v for k, v in value.items() if k != "evidence_hash"}
    if (value.get("schema") != 1 or value.get("date") != day or not value.get("sealed")
            or value.get("evidence_hash") != digest(unsigned)):
        raise ValueError("研究证据日期或哈希不一致")
    return value


def collect_research(root, day, state=None, config=None, client=None):
    root = Path(root)
    date.fromisoformat(day)
    cfg = validate_config(config or read_json(DEFAULT_CONFIG))
    path = root / "research/data" / f"{day}.json"
    captured = now()
    # Archived snapshots are immutable. Never replace them with current data on a rerun.
    if path.exists():
        return check_archive(read_json(path), day)
    symbols, total_symbols = focus_symbols(state or {}, day, cfg["max_symbols"])
    doc = {"schema": 1, "date": day, "captured_at": captured.isoformat(), "sealed": False,
           "scope": "盘后研究资料，不是选股输入或交易指令；只代表采集时可见信息。",
           "symbols": symbols, "symbol_count": total_symbols, "symbol_limit": cfg["max_symbols"], "datasets": {}}
    if not cfg["enabled"]:
        reason = "disabled"
    elif day != captured.date().isoformat():
        reason = "historical_archive_missing"
    elif captured.time().replace(tzinfo=None) < time(15, 10):
        reason = "before_research_collection_window"
    else:
        reason = None
    if reason:
        doc["datasets"] = {k: missing(k, reason) for k in LABELS}
        # Do not persist this: a skipped/old review must not create counterfeit historical evidence.
        doc["status"] = reason
        return doc
    client = client or PublicClient(cfg)
    for kind, providers in cfg["sources"].items():
        entry = missing(kind, "no_available_source")
        selected = symbols
        if kind == "dragon_tiger_seats":
            lhb = doc["datasets"].get("dragon_tiger", {})
            if lhb.get("status") not in ("ok", "partial"):
                doc["datasets"][kind] = missing(kind, "dragon_tiger_unavailable")
                continue
            listed = {r["code"] for r in lhb.get("rows", [])}
            selected = [s for s in symbols if s in listed]
            if not lhb.get("complete"):
                # A partial list cannot prove that unlisted candidates had no disclosure.
                doc["datasets"][kind] = missing(kind, "dragon_tiger_incomplete")
                continue
        if kind in ("dragon_tiger_seats", "announcements", "financials") and not selected:
            entry.update(status="not_applicable", reason="no_relevant_symbols", row_count=0)
        else:
            attempts = []
            for provider in providers:
                if provider == "archive":
                    attempts.append({"source": provider, "status": "no_same_day_archive"})
                    continue
                try:
                    if now().date().isoformat() != day:
                        raise FeedError("date_changed")
                    data = fetch(client, kind, day, selected, now())
                    entry = {"label": LABELS[kind], "source": provider, "trade_date": day,
                             "fetched_at": now().isoformat(), "published_at": None, **data}
                    if not data["rows"] and data.get("errors"):
                        entry.update(status="missing", row_count=None, reason="all_requests_failed")
                    entry["data_hash"] = digest(data)
                    attempts.append({"source": provider, "status": entry["status"]})
                    break
                except FeedError as exc:
                    attempts.append({"source": provider, "status": exc.code})
                    entry = missing(kind, exc.code)
                except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
                    attempts.append({"source": provider, "status": "invalid_schema"})
                    entry = missing(kind, "invalid_schema")
            entry["attempts"] = attempts
        doc["datasets"][kind] = entry
        print(f"研究数据：{LABELS[kind]} {entry['status']}（{entry.get('row_count') if entry.get('row_count') is not None else '未知'}条）", flush=True)
    doc.update(sealed=True, completed_at=now().isoformat(), status="collected",
               request_count=client.requests, provider_circuits=client.blocked)
    doc["evidence_hash"] = digest(doc)
    write_json(path, doc)
    return doc


def summary(doc, max_chars=20000):
    """Give every category a bounded share; never silently imply the displayed sample is complete."""
    out = {k: doc.get(k) for k in ("date", "captured_at", "scope", "evidence_hash", "symbol_count", "symbol_limit")}
    out["datasets"] = {}
    per_feed = max(400, (max_chars - 1800) // len(LABELS))
    symbols = set(doc.get("symbols", []))
    for kind, entry in doc["datasets"].items():
        selected = {k: entry[k] for k in ("label", "source", "status", "reason", "row_count", "total", "complete", "fetched_at", "note", "data_hash") if k in entry}
        selected["error_count"] = len(entry.get("errors", {}))
        selected["rows"] = []
        rows = sorted(entry.get("rows", []), key=lambda r: r.get("code") not in symbols)
        for row in rows[:12]:
            trial = {**selected, "rows": selected["rows"] + [row]}
            if len(json.dumps(trial, ensure_ascii=False)) > per_feed:
                break
            selected["rows"].append(row)
        selected["shown_rows"] = len(selected["rows"])
        selected["sample_truncated"] = len(selected["rows"]) < len(rows)
        out["datasets"][kind] = selected
    return out


def add_to_facts(facts, doc, max_chars=20000):
    facts = copy.deepcopy(facts)
    facts["research_data"] = summary(doc, max_chars)
    facts["limitations"] = [x for x in facts["limitations"] if x != "没有提供新闻或公司公告，不得编造归因"]
    facts["limitations"].append("研究资料按类别标注完整性和时点；快讯、公告索引、财务摘要均有限覆盖，不证明涨跌因果；不得用于改写当天盘前决策。")
    absent = [e["label"] for e in doc["datasets"].values() if e["status"] == "missing"]
    if absent:
        facts["limitations"].append("缺少可用研究证据：" + "、".join(absent))
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(description="仅检查公开研究接口；不访问R2、账户或AI")
    parser.add_argument("--state-dir", required=True, type=Path, help="独立检查输出目录")
    parser.add_argument("--date", default=now().date().isoformat())
    parser.add_argument("--symbols", default="", help="逗号分隔的六位股票代码；不会读取账户")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    if (args.state_dir / "account.json").exists() or (args.state_dir / "meeting").exists():
        parser.error("接口检查请使用独立目录，不使用账户运行目录")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if any(not re.fullmatch(r"\d{6}", s) for s in symbols):
        parser.error("股票代码必须为六位数字")
    doc = collect_research(args.state_dir, args.date, {"positions": [{"code": s} for s in symbols]}, read_json(args.config))
    print(json.dumps({"date": doc["date"], "status": doc["status"], "datasets": {k: {x: e.get(x) for x in ("status", "row_count", "reason")} for k, e in doc["datasets"].items()}}, ensure_ascii=False, indent=2))
    if doc["status"] != "collected" or any(x["status"] == "missing" for x in doc["datasets"].values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
