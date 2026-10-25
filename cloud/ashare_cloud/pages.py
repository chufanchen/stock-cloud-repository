"""Read-only static publishing. Export presentation fields, never raw state or model context."""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path

from .common import file_hash, now, read_json
from .storage import Store

ASSETS = Path(__file__).resolve().parents[1] / "web"
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
STATUS = {"ok": "复盘完成", "failed": "复盘待重试", "running": "复盘进行中", "disabled": "仅程序结算"}


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def number(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def money(value, signed=False):
    value = number(value)
    return "—" if value is None else format(value, "+,.2f" if signed else ",.2f")


def tone(value):
    v = number(value)
    return "gain" if v is not None and v > 0 else "loss" if v is not None and v < 0 else ""


def text(value, limit=24000):
    return value[:limit] if isinstance(value, str) else ""


def string_list(value):
    return [text(v, 2000) for v in value[:20] if isinstance(v, str)] if isinstance(value, list) else []


def selected_rows(rows, fields):
    if not isinstance(rows, list):
        return []
    return [{k: (v if type(v) in (int, float, bool) and (not isinstance(v, float) or math.isfinite(v))
                  else text(v, 1500)) for k, v in row.items() if k in fields}
            for row in rows if isinstance(row, dict)]


def publication_path(name):
    return bool(re.fullmatch(r"reports/\d{4}-\d{2}-\d{2}\.json|meeting/days/\d{4}-\d{2}-\d{2}\.json|research/proposals\.json", name))


def pull_source(root, store):
    """Fetch only the small immutable state bundle. No database download or R2 writes."""
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise ValueError("网页数据恢复目录必须为空")
    root.mkdir(parents=True, exist_ok=True)
    manifest = store.head()
    if manifest is None:
        return {"last_settled": None, "initialized": False}
    if not manifest["state_key"].startswith(store.key("states/")):
        raise ValueError("R2状态包位置不属于当前项目")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "state.tar.gz"
        store.client.download_file(store.bucket, manifest["state_key"], str(archive))
        if file_hash(archive) != manifest["state_sha256"]:
            raise ValueError("网页数据的R2状态包校验失败")
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if sum(m.size for m in members) > 512 * 1024 * 1024:
                raise ValueError("网页源状态包超过大小限制")
            for member in members:
                if (not member.isfile() or member.size > 64 * 1024 * 1024
                        or Path(member.name).is_absolute() or ".." in Path(member.name).parts):
                    raise ValueError("网页源状态包包含不安全路径或文件")
                if publication_path(member.name):
                    target = root / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as source, target.open("wb") as out:
                        shutil.copyfileobj(source, out)
    return {"last_settled": manifest.get("last_settled"), "initialized": True}


def load_public_data(root, limit=365):
    root = Path(root)
    reports = []
    for path in sorted((root / "reports").glob("*.json"))[-limit:]:
        if not DAY.fullmatch(path.stem):
            continue
        date.fromisoformat(path.stem)
        raw = read_json(path)
        facts, ai = raw["facts"], raw["ai"]
        if facts["date"] != path.stem or facts["settlement"]["date"] != path.stem:
            raise ValueError("日报文件名与结算日期不一致")
        settlement = facts["settlement"]
        report = {"date": path.stem, "status": ai.get("status", "failed"),
                  "content": text(ai.get("content")) if ai.get("status") == "ok" else "本日AI复盘尚未完成，账户数字以程序结算为准。",
                  **{k: number(settlement.get(k)) for k in ("equity", "cash", "market_value", "day_pnl", "realized_pnl")},
                  "coverage": number(facts.get("market_coverage")),
                  "buys": selected_rows(settlement.get("buys"), ("code", "name", "qty", "price")),
                  "sells": selected_rows(settlement.get("sells"), ("code", "name", "qty", "price", "pnl", "reason")),
                  "rejects": selected_rows(settlement.get("rejects"), ("code", "name", "reason")),
                  "positions": selected_rows(settlement.get("positions"), ("code", "name", "qty", "buy_price", "mark")),
                  "events": selected_rows(settlement.get("events"), ("code", "action", "reason")),
                  "limitations": string_list(facts.get("limitations"))}
        plan = facts.get("next_plan")
        report["next_plan"] = ({"date": text(plan.get("trade_date"), 10),
                               "count": number(plan.get("candidate_count")), "paused": plan.get("buy_paused") is True,
                               "picks": selected_rows(plan.get("picks"), ("code", "name", "pos", "prev_close"))} if plan else None)
        meeting_path = root / "meeting/days" / path.name
        report["meeting"] = []
        if meeting_path.exists():
            m = read_json(meeting_path)
            if m.get("date") != path.stem:
                raise ValueError("会议日期与日报不一致")
            for role in m.get("settings", {}).get("roles", []) + [{"id": "moderator", "name": "会议主持人"}]:
                stage = m.get("stages", {}).get(role["id"], {})
                d = stage.get("data", {})
                report["meeting"].append({"name": text(role.get("name"), 100), "status": stage.get("status", "running"),
                                          "summary": text(d.get("summary")),
                                          **{k: string_list(d.get(k)) for k in ("observations", "questions", "agreements", "disagreements")}})
        reports.append(report)
    proposals = []
    proposal_file = root / "research/proposals.json"
    if proposal_file.exists():
        for p in read_json(proposal_file).get("proposals", []):
            proposals.append({**{k: text(p.get(k), 1500) for k in ("id", "title", "hypothesis", "created_date", "updated_date")},
                              "observations": selected_rows(p.get("observations"),
                                  ("date", "evidence", "test_plan", "rejection_criterion", "data_needed"))})
    return {"reports": reports, "proposals": proposals}


def prose(content):
    """Small readable Markdown subset, deliberately without HTML, image or link evaluation."""
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading:
            lines.append("<h3>" + inline(heading.group(1)) + "</h3>")
        elif line.startswith(("- ", "* ")):
            lines.append('<p class="bullet">' + inline(line[2:]) + "</p>")
        else:
            lines.append("<p>" + inline(line) + "</p>")
    return '<div class="prose">' + "".join(lines) + "</div>"


def inline(value):
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", esc(value))


def excerpt(value):
    return re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+)", "", value).replace("**", "")[:220]


def badge(status):
    return '<span class="badge ' + ("ready" if status == "ok" else "pending") + '">' + esc(STATUS.get(status, "待更新")) + "</span>"


def empty(title, description):
    return f'<section class="empty"><span class="empty-mark">↗</span><h2>{esc(title)}</h2><p>{esc(description)}</p></section>'


def shell(title, subtitle, body, active="overview", prefix="", demo=False, updated=None):
    navigation = [("overview", "index.html", "01", "每日总览"), ("history", "history.html", "02", "历史日报"),
                  ("meeting", "meeting.html", "03", "AI 会议室"), ("research", "research.html", "04", "研究提案")]
    nav = "".join(f'<a class="nav-item {"active" if key == active else ""}" href="{prefix}{url}"><span>{n}</span>{label}</a>'
                  for key, url, n, label in navigation)
    banner = '<div class="demo-banner">页面设计预览 · 以下为演示数据，不代表实际交易或模型输出</div>' if demo else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="description" content="A股模拟交易的每日复盘、AI讨论与待验证研究。">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'none'">
<title>{esc(title)} · 无名游资复盘台</title><link rel="stylesheet" href="{prefix}assets/site.css"><script defer src="{prefix}assets/site.js"></script></head>
<body>{banner}<a class="skip" href="#main">跳到正文</a><div class="layout"><aside class="sidebar">
<a class="brand" href="{prefix}index.html"><span class="brand-icon">无</span><span>无名游资<small>交易研究 · 每日记录</small></span></a>
<div class="nav-label">研究档案 / RESEARCH LOG</div><nav aria-label="主导航">{nav}</nav>
<div class="sidebar-note"><span class="live-dot"></span> 模拟账户<small>记录事实，保留分歧。<br>研究建议等待验证。</small></div></aside>
<main id="main"><header class="topline"><span>收盘记录 / DAILY REVIEW</span><span class="pill">A 股 · 模拟研究</span></header>
<div class="page-heading"><div><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div><div class="edition">RESEARCH<br><strong>NOTEBOOK</strong></div></div>
{body}<footer><span>无名游资 · 复盘台</span><span>{'站点最新日报 ' + esc(updated) if updated else '等待首份复盘'} · 模拟交易，AI观点待验证</span></footer>
</main></div></body></html>'''


def cards(report):
    items = [("账户总资产", "equity", False, "模拟资金 · 元"), ("当日损益", "day_pnl", True, "较上一结算日 · 元"),
             ("可用现金", "cash", False, "结算后余额 · 元"), ("持仓市值", "market_value", False, "按收盘价估值 · 元")]
    return '<div class="metrics">' + "".join(f'<section class="metric"><div>{label}</div><strong class="{tone(report[k]) if signed else ""}">{money(report[k], signed)}</strong><small>{note}</small></section>' for label, k, signed, note in items) + "</div>"


def trend(reports):
    points = [(r["date"], r["equity"]) for r in reports if r["equity"] is not None][-60:]
    if len(points) < 2:
        return '<div class="chart-empty">累计两个结算日后显示资产走势</div>'
    low, high = min(v for _, v in points), max(v for _, v in points)
    gap = max(high - low, 10)
    low, high = low - gap * .15, high + gap * .15
    coords = [(74 + i * 606 / (len(points) - 1), 162 - (v - low) * 134 / (high - low)) for i, (_, v) in enumerate(points)]
    grid = "".join(f'<line x1="74" x2="680" y1="{y}" y2="{y}" class="grid-line"/><text x="0" y="{y+4}" class="axis-label">{money(low+(162-y)*(high-low)/134)}</text>' for y in (28, 95, 162))
    path = " ".join(f'{x:.2f},{y:.2f}' for x, y in coords)
    area = f'74,170 {path} 680,170'
    return f'<svg class="chart" viewBox="0 0 710 205" role="img" aria-label="最近{len(points)}个结算日账户资产走势">{grid}<polygon points="{area}" class="chart-area"/><polyline points="{path}" class="chart-line"/><circle cx="{coords[-1][0]:.2f}" cy="{coords[-1][1]:.2f}" r="4" class="chart-dot"/><text x="74" y="198" class="axis-label">{points[0][0]}</text><text x="605" y="198" class="axis-label">{points[-1][0]}</text></svg>'


def table(headers, rows):
    if not rows:
        return '<p class="muted table-empty">暂无记录</p>'
    return '<div class="table-wrap"><table><thead><tr>' + "".join(f'<th>{esc(h)}</th>' for h in headers) + '</tr></thead><tbody>' + "".join('<tr>' + "".join(f'<td>{esc(v)}</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def history_rows(reports, prefix="", limit=None):
    rows = reports[::-1][:limit]
    return '<div class="report-list">' + "".join(f'<a class="report-row" data-filter-row href="{prefix}days/{r["date"]}.html"><span class="date-tile">{r["date"][5:].replace("-", ".")}<small>{r["date"][:4]}</small></span><span class="report-title">收盘复盘<small>买入 {len(r["buys"])} · 卖出 {len(r["sells"])} · 未买入 {len(r["rejects"])}</small></span>{badge(r["status"])}<strong class="{tone(r["day_pnl"])}">{money(r["day_pnl"], True)}</strong><span class="arrow">↗</span></a>' for r in rows) + '</div>'


def candidate_table(report):
    p = report["next_plan"]
    if not p:
        return '<p class="muted">本次没有生成下一交易日计划。</p>'
    notice = f'<p class="muted">交易日 {esc(p["date"])} · 共 {esc(p["count"])} 个候选' + (' · 暂停新增买入' if p["paused"] else '') + '</p>'
    rows = [[p.get("code"), p.get("name"), f'{p["pos"]:.1%}' if number(p.get("pos")) is not None else "—", money(p.get("prev_close"))] for p in p["picks"]]
    return notice + table(["代码", "名称", "120日区间位", "前收盘价"], rows)


def day_body(report, reports):
    r = report
    body = f'<div class="day-meta"><span>{r["date"]} · 收盘后</span>{badge(r["status"])}</div>' + cards(r)
    body += '<section class="panel"><div class="section-title"><h2>AI 复盘</h2><a href="../meetings/' + r['date'] + '.html">查看各席发言 ↗</a></div>' + prose(r["content"]) + '</section>'
    body += '<div class="two-col"><section class="panel"><h2>当前持仓</h2>' + table(["股票", "股数", "买入价", "收盘价"],
        [[p.get("name") or p.get("code"), p.get("qty"), money(p.get("buy_price")), money(p.get("mark"))] for p in r["positions"]]) + '</section>'
    body += '<section class="panel"><h2>下一交易日候选</h2>' + candidate_table(r) + '</section></div>'
    body += '<section class="panel"><h2>模拟成交与未买入原因</h2>' + table(["类型", "股票", "股数", "价格", "说明"],
        [[label, p.get("name") or p.get("code"), p.get("qty", "—"), money(p.get("price")), p.get("reason", "按冻结计划执行")]
         for key, label in (("buys", "买入"), ("sells", "卖出"), ("rejects", "未买入")) for p in r[key]])
    body += f'<p class="muted">本日平仓交易的累计已实现损益：{money(r["realized_pnl"], True)} 元，与当日损益口径不同。</p></section>'
    body += '<section class="panel quiet"><h2>数据与口径</h2>' + prose("\n".join("- " + x for x in r["limitations"])) + '</section>'
    return body


def meeting_body(report, prefix=""):
    if not report or not report["meeting"]:
        return empty("会议尚未开始", "逐席讨论完成后，这里会展示独立观点、交叉审计和汇总结论。")
    body = '<p class="section-note">四席独立分析 → 交叉审计 → 汇总。以下观点尚未经过自动回测验证。</p>'
    for i, stage in enumerate(report["meeting"]):
        body += f'<article class="panel seat"><div class="seat-number">{i+1:02d}</div><div class="seat-main"><div class="section-title"><h2>{esc(stage["name"])}</h2>{badge(stage["status"])}</div>'
        body += prose(stage["summary"]) if stage["status"] == "ok" else '<p class="muted">本席尚未完成，已完成发言将保留。</p>'
        for key, label in (("observations", "观察与证据"), ("questions", "待回答的问题"), ("agreements", "共识"), ("disagreements", "分歧")):
            if stage[key]:
                body += '<h3>' + label + '</h3>' + prose("\n".join("- " + x for x in stage[key]))
        body += '</div></article>'
    return body


def build_site(root, output, source=None, demo=False):
    root, output = Path(root), Path(output)
    if output.resolve() == root.resolve() or output.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(output.resolve()):
        raise ValueError("网页输出必须与私有状态目录分开")
    if output.exists() and any(output.iterdir()):
        raise ValueError("网页输出目录必须为空，避免夹带历史文件或密钥")
    data = load_public_data(root)
    reports, proposals = data["reports"], data["proposals"]
    latest = reports[-1] if reports else None
    updated = latest["date"] if latest else None
    output.mkdir(parents=True, exist_ok=True)
    (output / "assets").mkdir()
    for name in ("site.css", "site.js"):
        shutil.copyfile(ASSETS / name, output / "assets" / name)
    def save(path, title, subtitle, body, active="overview", prefix=""):
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shell(title, subtitle, body, active, prefix, demo, updated), encoding="utf-8")
    pending = source and source.get("last_settled") and (not updated or source["last_settled"] > updated)
    alert = '<div class="notice">已有较新的模拟结算，完整复盘仍在生成。以下展示最近一份已归档日报。</div>' if pending else ""
    if latest:
        body = alert + f'<div class="day-meta"><span>{updated} · 最新收盘记录</span>{badge(latest["status"])}</div>' + cards(latest)
        body += '<div class="overview-grid"><section class="panel"><div class="section-title"><h2>账户资产走势</h2><span class="muted">最近60个结算日 · 元</span></div>' + trend(reports) + '</section>'
        body += f'<section class="panel featured"><span class="eyebrow">LATEST REVIEW</span><h2>今日复盘</h2><p>{esc(excerpt(latest["content"]))}</p><a class="button" href="days/{updated}.html">阅读完整复盘 <span>↗</span></a></section></div>'
        body += '<div class="two-col"><section class="panel"><div class="section-title"><h2>最近记录</h2><a href="history.html">全部记录 ↗</a></div>' + history_rows(reports, limit=4) + '</section>'
        body += '<section class="panel"><div class="section-title"><h2>下一交易日观察</h2><span class="pill">候选 ≠ 成交</span></div>' + candidate_table(latest) + '</section></div>'
    else:
        body = alert + empty("等待第一份收盘复盘", "每个交易日完成模拟结算与AI讨论后，最新记录会自动出现在这里。当前没有可展示的日报，不显示虚构收益。")
        body += '<div class="intro-grid">' + "".join(f'<section class="panel"><span class="eyebrow">{n}</span><h2>{title}</h2><p class="muted">{desc}</p></section>' for n, title, desc in
                 [("01 / RECORD", "每日结算", "记录模拟资产、成交与未买入原因。"), ("02 / DISCUSS", "多角色复盘", "独立分析，交叉质疑，保留不同意见。"), ("03 / RESEARCH", "研究提案", "把新想法写成可以验证的假设。")]) + '</div>'
    save("index.html", "每日复盘", "看清交易结果，也记录判断的依据。", body)
    history = '<div class="section-title"><p class="muted">共 ' + str(len(reports)) + ' 份归档日报</p><label class="search">查找日期<input type="search" data-filter placeholder="例如 2026-10" aria-label="查找日报日期"></label></div>'
    history += history_rows(reports) if reports else empty("还没有历史日报", "第一份复盘生成后即可按日期回看。")
    save("history.html", "历史日报", "按时间回看每一次结算、复盘与选择。", history, "history")
    selector = '<div class="date-links">' + "".join(f'<a href="meetings/{r["date"]}.html">{r["date"]}</a>' for r in reports[-12:][::-1]) + '</div>'
    save("meeting.html", "AI 会议室", (updated + " · 最新讨论") if updated else "独立观点，交叉审计，保留分歧。", selector + meeting_body(latest), "meeting")
    research = '<div class="research-intro"><strong>' + str(len(proposals)) + '</strong><p>个待验证提案<br><span>讨论形成假设，验证决定下一步。交易规则不会自动改动。</span></p></div>'
    for i, p in enumerate(proposals[::-1]):
        research += f'<article class="panel proposal"><div class="section-title"><span class="eyebrow">RESEARCH {i+1:02d}</span><span class="badge pending">待验证</span></div><h2>{esc(p["title"])}</h2><p>{esc(p["hypothesis"])}</p><p class="muted">首次 {esc(p["created_date"])} · 最近讨论 {esc(p["updated_date"])}</p>'
        for observation in p["observations"]:
            research += '<details><summary>' + esc(observation.get("date")) + ' · 验证设计</summary>'
            for key, label in (("evidence", "已有证据与局限"), ("test_plan", "验证方案"), ("rejection_criterion", "否定条件"), ("data_needed", "所需数据")):
                research += '<h3>' + label + '</h3>' + prose(text(observation.get(key)))
            research += '</details>'
        research += '</article>'
    if not proposals:
        research += empty("暂无待验证提案", "会议中形成的新假设会在这里登记，包含验证方案和否定条件。")
    save("research.html", "研究提案", "让每个想法都有可验证的下一步。", research, "research")
    for r in reports:
        save(f'days/{r["date"]}.html', "收盘复盘", r["date"] + " · 当日归档", day_body(r, reports), "history", "../")
        save(f'meetings/{r["date"]}.html', "AI 会议室", r["date"] + " · 当日讨论", meeting_body(r), "meeting", "../")
    (output / ".nojekyll").write_text("")
    (output / "publication.json").write_text(json.dumps({"generated_at": now().isoformat(), "latest_report": updated,
        "report_count": len(reports), "proposal_count": len(proposals), "demo": demo}, ensure_ascii=False), encoding="utf-8")
    print(f"静态网页已生成：{len(reports)} 份日报，{len(proposals)} 个研究提案。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="从R2或本地归档只读生成GitHub Pages静态网页")
    parser.add_argument("--state-dir", type=Path, help="使用本地归档；省略则读取R2")
    parser.add_argument("--output", type=Path, default=Path("_site"))
    args = parser.parse_args(argv)
    if args.state_dir:
        build_site(args.state_dir, args.output)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            source = pull_source(root, Store())
            build_site(root, args.output, source=source)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        from botocore.exceptions import BotoCoreError, ClientError
        if isinstance(exc, (BotoCoreError, ClientError)):
            print("ERROR: 网页构建无法读取R2，请检查端点和读取权限。", file=sys.stderr)
        elif isinstance(exc, (ValueError, RuntimeError)):
            print("ERROR: " + str(exc), file=sys.stderr)
        else:
            print("ERROR: 网页源数据格式不兼容：" + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
