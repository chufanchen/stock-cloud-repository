from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from .common import Calendar, digest, file_hash, now, read_json, validate_config, write_json
from .engine import build_plan, simulate, verify_plan
from .market import collect, scan_database, seed_database
from .meeting import DEFAULT_CONFIG as MEETING_CONFIG, MeetingError, run_meeting, validate_config as validate_meeting
from .review import ai_review, evidence, render
from .research_data import DEFAULT_CONFIG as RESEARCH_CONFIG, add_to_facts, collect_research
from .storage import Store

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.json"


def check_account(state):
    if state.get("schema") != 1 or state["cash"] < 0:
        raise ValueError("账本格式或现金错误")
    prev = None
    for _, plan in sorted(state["plans"].items()):
        verify_plan(plan)
        if plan["previous_plan_hash"] != prev:
            raise ValueError("计划哈希链断裂")
        prev = plan["hash"]
    if prev != state.get("last_plan_hash"):
        raise ValueError("计划链尾不一致")


def initialize(root, legacy, cfg):
    root, legacy = Path(root), Path(legacy)
    if root.exists() and any(root.iterdir()):
        raise ValueError("初始化目标必须为空；不会重置已有云端账本")
    # This distributed snapshot is flat. Validate rather than infer a portfolio from prose.
    seed_fill = read_json(legacy / "data/fills/20261019.json")
    value = seed_fill.get("market_value", seed_fill.get("hold_market_value"))
    equity = seed_fill.get("total_asset", seed_fill.get("asset_after"))
    cash = seed_fill.get("cash", seed_fill.get("cash_after"))
    if (value != 0 or equity is None or cash is None or abs(cash - equity) > 0.011
            or abs(equity - cfg["initial_cash"]) > 0.011 or seed_fill.get("forced_hold_n", 0)):
        raise ValueError("原始账本已变更或存在持仓，请明确迁移持仓后再初始化")
    root.mkdir(parents=True, exist_ok=True)
    meta = seed_database(legacy / "data/market.db", root / "market.db", cfg["seed_sessions"])
    state = {"schema": 1, "created_at": now().isoformat(), "cash": cfg["initial_cash"],
             "equity": cfg["initial_cash"], "positions": [], "closed_trades": [],
             "last_settled": None, "settlements": {}, "plans": {}, "last_plan_hash": None,
             "provenance": {**meta, "original_seed_cash": cfg["initial_cash"],
                            "original_fill_sha256": file_hash(legacy / "data/fills/20261019.json"),
                            "note": "承接原10/19空仓现金；云端观察期另计，不事后补建旧计划"}}
    write_json(root / "account.json", state)
    print(f"初始化完成：{meta['stock_count']} 只，历史 {meta['seed_start']} 至 {meta['seed_end']}；现金 {state['cash']:.2f}")


def prepare_observation(root, day, cfg, calendar, state, save):
    start = state.get('provenance', {}).get('observation_start')
    if not start or state['settlements'] or calendar.next(day) != start or day != now().date().isoformat():
        raise ValueError('只能用新观察期前一交易日的实际收盘行情准备首日计划')
    if start in state['plans']:
        verify_plan(state['plans'][start])
        print('新观察期首日计划已冻结，不重复选股。')
        return
    collect(root, day, cfg, calendar, **({'checkpoint':save} if save else {}))
    scan = scan_database(root / 'market.db', day, cfg, calendar)
    write_json(root / 'scans' / f'{day}.json', scan)
    plan = build_plan(day, scan, state, cfg, calendar, now().isoformat())
    state['plans'][start] = plan
    state['last_plan_hash'] = plan['hash']
    write_json(root / 'account.json', state)
    if save:
        save()
    print(f'已冻结 {start} 首日计划；观察期尚未开始，没有生成结算或收益。')


def execute(root, day, cfg, calendar, store=None, skip_ai=False, meeting_cfg=None, review_only=False, checkpoint=None):
    root = Path(root)
    save = (lambda: store.push(root)) if store else checkpoint
    state = read_json(root / "account.json")
    check_account(state)
    start = state.get('provenance', {}).get('observation_start')
    if start and day < start and not review_only:
        return prepare_observation(root, day, cfg, calendar, state, save)
    if start and day >= start and not state['settlements'] and (day != start or day not in state['plans']):
        raise ValueError('新观察期首日缺少冻结计划或错过首日，停止；不得事后补造')
    if review_only and day not in state["settlements"]:
        raise ValueError("该日尚未结算，不能单独复盘；先运行 run")
    if not calendar.is_session(day):
        print(f"{day} 为休市日，跳过。")
        return
    saved_review = root / "reviews" / f"{day}.json"
    if day in state["settlements"]:
        market = read_json(root / "snapshots" / f"{day}.json")
        print(f"{day} 已结算；本次仅检查或补生成报告。")
    else:
        plan = state["plans"].get(day)
        if store and plan and "tencent_snapshot" in cfg.get("auction_sources", ["tushare", "tencent_snapshot"]):
            from .auction import restore_capture
            try:
                restore_capture(store, root, plan, day)
            except Exception:
                # Auction evidence is optional. Never replace it with current turnover.
                print("竞价快照未能恢复；继续其他来源，无可靠证据的候选将拒绝买入。", flush=True)
        market = collect(root, day, cfg, calendar,
                         **({"checkpoint": save} if save else {}))
        # Commit the exact-day market evidence first. A later engine failure remains recoverable.
        if save:
            save()
        if not plan and state["last_settled"]:
            raise ValueError("该交易日缺少盘前冻结计划，禁止盘后临时选股成交")
        new_state, settlement = simulate(state, plan, market, day, calendar)
        scan = scan_database(root / "market.db", day, cfg, calendar)
        write_json(root / "scans" / f"{day}.json", scan)
        # On archived recovery, never create an already started trading day's plan.
        if calendar.next(day) > now().date().isoformat():
            next_plan = build_plan(day, scan, new_state, cfg, calendar, now().isoformat())
            new_state["plans"][next_plan["trade_date"]] = next_plan
            new_state["last_plan_hash"] = next_plan["hash"]
        write_json(root / "account.json", new_state)
        state = new_state
        if save:
            save()
        print(f"模拟结算已保存：总资产 {settlement['equity']:.2f}，当日 {settlement['day_pnl']:+.2f}")
    facts = evidence(state, day, market)
    # Existing meetings (including failed ones) keep their exact inputs across upgrades.
    meeting_path = root / "meeting/days" / f"{day}.json"
    report_path = root / "reports" / f"{day}.json"
    frozen_facts = None
    if meeting_path.exists():
        frozen_facts = read_json(meeting_path)["input"]["facts"]
    elif saved_review.exists() and read_json(saved_review).get("status") == "ok" and report_path.exists():
        frozen_facts = read_json(report_path)["facts"]
    if frozen_facts is not None:
        if any(frozen_facts.get(k) != v for k, v in facts.items() if k != "limitations"):
            raise ValueError("已归档复盘的账户或行情事实发生变化，停止复用")
        facts = copy.deepcopy(frozen_facts)
    elif not skip_ai:
        research_cfg = read_json(RESEARCH_CONFIG)
        research = collect_research(root, day, state, research_cfg)
        facts = add_to_facts(facts, research, research_cfg["max_context_chars"])
        if save:
            save()
    meeting_cfg = validate_meeting(meeting_cfg if meeting_cfg is not None else read_json(MEETING_CONFIG))
    error = None
    if skip_ai:
        # Preserve successful output during a settlement-only run.
        review = read_json(saved_review) if saved_review.exists() else {"status": "disabled", "content": "本次显式跳过 AI，仅展示程序计算结果。"}
    elif meeting_cfg["enabled"]:
        try:
            review = run_meeting(root, facts, meeting_cfg, checkpoint=save)
        except MeetingError as exc:
            error = str(exc)
            review = {"status": "failed", "kind": "meeting", "error": error, "generated_at": now().isoformat()}
    elif saved_review.exists() and read_json(saved_review).get("status") == "ok":
        review = read_json(saved_review)
    else:
        try:
            review = ai_review(facts)
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
            review = {"status": "failed", "error": error, "generated_at": now().isoformat()}
    write_json(saved_review, review)
    render(root, facts, review)
    if save:
        save()
    if error and cfg["ai_required"]:
        raise RuntimeError(error + "；账本和失败状态已保存，重跑只补复盘。")


def main(argv=None, checkpoint=None):
    parser = argparse.ArgumentParser(description="A股每日模拟盘与研究会议：服务器 / GitHub Actions + R2")
    parser.add_argument("command", choices=("init", "bootstrap", "run", "review", "sync", "status", "restore"))
    parser.add_argument("--state-dir", type=Path, default=Path(".cloud-state"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--meeting-config", type=Path, default=MEETING_CONFIG)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--date", help="YYYY-MM-DD；历史补跑要求已有归档快照")
    parser.add_argument("--local", action="store_true", help="使用本地状态，不读写 R2")
    parser.add_argument("--skip-ai", action="store_true", help="仅用于检查，显式不生成 AI 复盘")
    args = parser.parse_args(argv)
    cfg = validate_config(read_json(args.config))
    meeting_cfg = validate_meeting(read_json(args.meeting_config))
    root = args.state_dir
    if args.command == "init":
        if not args.legacy_root:
            parser.error("init 需要 --legacy-root")
        initialize(root, args.legacy_root, cfg)
    elif args.command == "bootstrap":
        check_account(read_json(root / "account.json"))
        store = Store()
        if store.head():
            raise ValueError("R2 已初始化；禁止再次覆盖。后续使用 run。")
        store.push(root, bootstrap=True)
        print("R2 初始化成功。")
    elif args.command == "restore":
        Store().pull(root)
        check_account(read_json(root / "account.json"))
        print("R2 状态恢复并校验完成。")
    elif args.command == "status":
        s = read_json(root / "account.json")
        check_account(s)
        print(json.dumps({k: s[k] for k in ("cash", "equity", "last_settled", "positions")}, ensure_ascii=False, indent=2))
    elif args.command == "sync":
        # Prepare history from an already archived close, even before today's close.
        # This command never creates a settlement, a plan or a model request.
        store = None
        if not args.local:
            store = Store()
            store.pull(root)
        candidates = sorted((root / "snapshots").glob("????-??-??.json"))
        day = args.date or (candidates[-1].stem if candidates else None)
        if not day:
            raise ValueError("没有归档收盘快照；先在交易日收盘后运行 run")
        from datetime import date
        date.fromisoformat(day)
        cal = Calendar()
        if not cal.is_session(day):
            raise ValueError("归档日期不是交易日")
        before = file_hash(root / "account.json")
        check_account(read_json(root / "account.json"))
        print(f"仅补齐 {day} 归档所需历史行情；不结算、不生成交易计划、不调用 AI。", flush=True)
        collect(root, day, cfg, cal, archive_only=True,
                checkpoint=(lambda: store.push(root)) if store else checkpoint)
        if file_hash(root / "account.json") != before:
            raise RuntimeError("历史补齐不应修改账户，停止发布")
        if store:
            store.push(root)
        elif checkpoint:
            checkpoint()
        print("历史行情补齐完成，账户与交易计划未变化。")
    else:
        day = args.date or now().date().isoformat()
        from datetime import date
        date.fromisoformat(day)
        cal = Calendar()
        if not cal.is_session(day):
            print(f"{day} 为休市日，跳过。")
            return
        if day > now().date().isoformat() or (day == now().date().isoformat() and now().hour < 15):
            raise ValueError("只能在收盘后运行；日期统一使用北京时间")
        store = None
        if not args.local:
            store = Store()
            store.pull(root)
        execute(root, day, cfg, cal, store, args.skip_ai, meeting_cfg, review_only=args.command == "review", checkpoint=checkpoint)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print provider response bodies, authorization headers or endpoint query strings.
        from botocore.exceptions import BotoCoreError, ClientError
        if isinstance(exc, (BotoCoreError, ClientError)):
            print("ERROR: R2 请求失败，请检查端点、权限和网络。", file=sys.stderr)
        elif isinstance(exc, (ValueError, RuntimeError)):
            print(f"ERROR: {exc}", file=sys.stderr)
        else:
            print(f"ERROR: {type(exc).__name__}；请检查配置和输入文件。", file=sys.stderr)
        sys.exit(1)
