"""Bounded public research feeds. Never used by the trading engine."""
from __future__ import annotations

import html
import math
import re
import time
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import requests

from .common import SHANGHAI

DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
LABELS = {"limit_up": "涨停池", "broken_limit_up": "炸板池", "limit_down": "跌停池",
          "dragon_tiger": "龙虎榜日榜", "dragon_tiger_seats": "龙虎榜席位",
          "sector_flow": "行业资金流", "themes": "题材归因", "news": "财经快讯",
          "announcements": "公司公告", "financials": "财务三表摘要"}
PROVIDERS = {k: "eastmoney" for k in LABELS} | {"themes": "ths"}


class FeedError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def clean(value, limit=240):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", html.unescape(str(value or "")))).strip()[:limit]


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def code(value):
    value = str(value or "")
    if not re.fullmatch(r"\d{6}", value):
        raise FeedError("invalid_stock_code")
    return value


def timestamp(value):
    # Some announcement times have a colon before milliseconds.
    value = str(value or "")[:19]
    try:
        return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)
    except ValueError:
        raise FeedError("invalid_source_time") from None


class PublicClient:
    def __init__(self, settings, session=None):
        self.settings = settings
        self.session = session or requests.Session()
        self.deadline = time.monotonic() + settings["budget_seconds"]
        self.last = 0.0
        self.requests = 0
        self.blocked = {}
        self.failures = {}

    def get(self, provider, url, params=None):
        host = urlparse(url).hostname or ""
        allowed = (host.endswith(".eastmoney.com") if provider == "eastmoney" else host == "zx.10jqka.com.cn")
        if not allowed or not url.startswith("https://"):
            raise FeedError("unsupported_endpoint")
        if provider in self.blocked:
            raise FeedError(self.blocked[provider])
        for attempt in range(2):
            pause = max(0, self.settings["request_interval"] - (time.monotonic() - self.last))
            if time.monotonic() + pause + 16 > self.deadline or self.requests >= self.settings["max_requests"]:
                raise FeedError("budget_exhausted")
            time.sleep(pause)
            self.requests += 1
            try:
                with self.session.get(url, params=params, headers={"User-Agent": "Mozilla/5.0",
                        "Referer": "https://data.eastmoney.com/" if provider == "eastmoney" else "https://www.10jqka.com.cn/"},
                        timeout=(5, 10), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if status in (401, 403, 429):
                        self.blocked[provider] = f"http_{status}"
                        raise FeedError(f"http_{status}")
                    if status != 200:
                        if status >= 500 and attempt == 0:
                            continue
                        raise FeedError(f"http_{status}")
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 4 * 1024 * 1024 or time.monotonic() > self.deadline:
                            raise FeedError("response_limit")
                    import json
                    try:
                        result = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise FeedError("invalid_json") from None
                    if not isinstance(result, dict):
                        raise FeedError("invalid_schema")
                    self.failures[provider] = 0
                    return result
            except requests.RequestException:
                self.failures[provider] = self.failures.get(provider, 0) + 1
                if self.failures[provider] >= 2:
                    self.blocked[provider] = "network_circuit_open"
                if attempt or provider in self.blocked:
                    raise FeedError("network_error") from None
            finally:
                self.last = time.monotonic()
        raise FeedError("network_error")


def result(rows, total=None, complete=True, **kwargs):
    return {"rows": rows, "row_count": len(rows), "total": total,
            "complete": complete, "status": "ok" if complete else "partial", **kwargs}


def datacenter(client, report, filters, sort, *, max_pages=8, page_size=500):
    rows, total, pages = [], None, None
    for page in range(1, max_pages + 1):
        data = client.get("eastmoney", DC, {"reportName": report, "columns": "ALL", "filter": filters,
            "pageNumber": page, "pageSize": page_size, "sortColumns": sort, "sortTypes": "-1", "source": "WEB", "client": "WEB"})
        if data.get("success") is not True or not isinstance(data.get("result"), dict):
            # A provider failure/null result is unknown, never a fabricated zero.
            raise FeedError("provider_no_result")
        node = data["result"]
        if (not isinstance(node.get("data"), list) or any(not isinstance(r, dict) for r in node["data"])
                or type(node.get("count")) is not int or type(node.get("pages")) is not int
                or node["count"] < 0 or node["pages"] < 0):
            raise FeedError("invalid_pagination")
        if total is not None and (total != node["count"] or pages != node["pages"]):
            raise FeedError("unstable_pagination")
        total, pages = node["count"], node["pages"]
        rows.extend(node["data"])
        if page >= pages:
            break
    return rows, total, len(rows) == total


def daily_lhb_row(row, day):
    if str(row.get("TRADE_DATE", ""))[:10] != day:
        raise FeedError("wrong_source_date")
    return {"code": code(row.get("SECURITY_CODE")), "name": clean(row.get("SECURITY_NAME_ABBR"), 40),
            "trade_date": day, "reason": clean(row.get("EXPLANATION")), "trade_id": clean(row.get("TRADE_ID"), 60),
            "buy_yuan": number(row.get("BILLBOARD_BUY_AMT")), "sell_yuan": number(row.get("BILLBOARD_SELL_AMT")),
            "net_yuan": number(row.get("BILLBOARD_NET_AMT")), "change_pct": number(row.get("CHANGE_RATE")),
            "turnover_pct": number(row.get("TURNOVERRATE"))}


def fetch_dragon_tiger(client, day, symbols, captured):
    rows, total, complete = datacenter(client, "RPT_DAILYBILLBOARD_DETAILSNEW", f"(TRADE_DATE='{day}')", "BILLBOARD_NET_AMT")
    normalized = [daily_lhb_row(r, day) for r in rows]
    keys = [(r["code"], r["reason"], r["trade_id"]) for r in normalized]
    if len(keys) != len(set(keys)):
        raise FeedError("duplicate_records")
    return result(normalized, total, complete, note="记录数不等于股票数；仅用于盘后研究；已剔除上榜后的涨幅等字段。")


def fetch_seats(client, day, symbols, captured):
    rows, errors = [], {}
    for symbol in symbols:
        for side in ("BUY", "SELL"):
            try:
                raw, total, complete = datacenter(client, "RPT_BILLBOARD_DAILYDETAILS" + side,
                    f'(TRADE_DATE=\'{day}\')(SECURITY_CODE="{symbol}")', side, max_pages=4, page_size=100)
                if not complete:
                    raise FeedError("incomplete_pages")
                for r in raw:
                    if str(r.get("TRADE_DATE", ""))[:10] != day or code(r.get("SECURITY_CODE")) != symbol:
                        raise FeedError("wrong_source_identity")
                rows.extend({"code": symbol, "trade_date": day, "side": side.lower(),
                    "reason": clean(r.get("EXPLANATION")), "trade_id": clean(r.get("TRADE_ID"), 60),
                    "seat_code": clean(r.get("OPERATEDEPT_CODE"), 60), "seat": clean(r.get("OPERATEDEPT_NAME"), 100),
                    "buy_yuan": number(r.get("BUY")), "sell_yuan": number(r.get("SELL")), "net_yuan": number(r.get("NET"))} for r in raw)
            except FeedError as exc:
                errors[symbol + ":" + side.lower()] = exc.code
    return result(rows, complete=not errors, errors=errors,
                  note="仅采选中股票；每个统计原因/区间独立保留；两侧席位可能重叠，禁止直接相加NET或推断自然人身份。")


def fetch_pool(client, kind, day):
    endpoint = {"limit_up": "getTopicZTPool", "broken_limit_up": "getTopicZBPool", "limit_down": "getTopicDTPool"}[kind]
    data = client.get("eastmoney", "https://push2ex.eastmoney.com/" + endpoint,
        {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt", "Pageindex": 0,
         "pagesize": 600, "sort": "fund:asc" if kind == "limit_down" else "fbt:asc", "date": day.replace("-", "")})
    node = data.get("data")
    if data.get("rc", 0) != 0 or not isinstance(node, dict) or not isinstance(node.get("pool"), list) or type(node.get("tc")) is not int:
        raise FeedError("provider_no_result")
    rows = [{"code": code(r.get("c")), "name": clean(r.get("n"), 40), "industry": clean(r.get("hybk"), 60),
             "change_pct": number(r.get("zdp")), "amount_yuan": number(r.get("amount")),
             "consecutive_boards": number(r.get("lbc")), "break_count": number(r.get("zbc")),
             "first_limit_time": clean(r.get("fbt"), 10), "last_limit_time": clean(r.get("lbt"), 10)} for r in node["pool"]]
    if len({r["code"] for r in rows}) != len(rows):
        raise FeedError("duplicate_records")
    return result(rows, node["tc"], len(rows) == node["tc"], note="供应商按请求日期返回的盘后池；当日现场归档，不用于历史补造。")


def fetch_sector(client, day, symbols, captured):
    rows, total = [], None
    for page in range(1, 13):
        d = client.get("eastmoney", "https://push2.eastmoney.com/api/qt/clist/get", {"pn": page, "pz": 100,
            "po": 1, "np": 1, "fltt": 2, "invt": 2, "fs": "m:90+t:2", "fid": "f62",
            "fields": "f3,f12,f14,f104,f105,f62,f184,f124"})
        node = d.get("data")
        if d.get("rc") != 0 or not isinstance(node, dict) or not isinstance(node.get("diff"), list) or type(node.get("total")) is not int:
            raise FeedError("provider_no_result")
        if total is not None and total != node["total"]:
            raise FeedError("unstable_pagination")
        total = node["total"]
        for r in node["diff"]:
            try:
                observed = datetime.fromtimestamp(float(r["f124"]), SHANGHAI)
            except (KeyError, ValueError, TypeError, OSError):
                raise FeedError("invalid_source_time") from None
            if observed.date().isoformat() != day or observed > captured + timedelta(minutes=2) or observed.hour < 15:
                raise FeedError("stale_or_intraday_snapshot")
            rows.append({"code": clean(r.get("f12"), 20), "name": clean(r.get("f14"), 60),
                         "observed_at": observed.isoformat(), "change_pct": number(r.get("f3")),
                         "main_net_yuan": number(r.get("f62")), "main_net_pct": number(r.get("f184")),
                         "up_count": number(r.get("f104")), "down_count": number(r.get("f105"))})
        if len(rows) >= total:
            break
    if len({r["code"] for r in rows}) != len(rows):
        raise FeedError("duplicate_records")
    return result(rows, total, len(rows) == total, note="行业分类按供应商定义；主力资金是订单分类统计，不代表已识别机构或个人。")


def fetch_themes(client, day, symbols, captured):
    d = client.get("ths", f"https://zx.10jqka.com.cn/event/api/getharden/date/{day}/orderby/date/orderway/desc/charset/GBK/")
    raw = d.get("data")
    if not isinstance(raw, list) or str(d.get("date", ""))[:10] != day:
        raise FeedError("wrong_source_date")
    rows = []
    for r in raw:
        if str(r.get("date", ""))[:10] != day:
            raise FeedError("wrong_source_date")
        rows.append({"code": code(r.get("code")), "name": clean(r.get("name"), 40), "date": day,
                     "attribution": clean(r.get("reason"), 220)})
    return result(rows, len(rows), note="供应商编辑归因，未经独立核实，不是已证实的因果。")


def fetch_news(client, day, symbols, captured):
    d = client.get("eastmoney", "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
        {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "", "pageSize": 30, "req_trace": str(uuid.uuid4())})
    raw = (d.get("data") or {}).get("fastNewsList")
    if str(d.get("code")) != "1" or not isinstance(raw, list):
        raise FeedError("provider_no_result")
    rows = []
    for r in raw:
        published = timestamp(r.get("showTime"))
        if published > captured + timedelta(minutes=2):
            raise FeedError("future_publication")
        if published.date().isoformat() == day:
            rows.append({"id": clean(r.get("code"), 40), "published_at": published.isoformat(), "title": clean(r.get("title"), 180)})
    return result(rows, complete=False, note="仅最近30条中的当日快讯标题，不是全天新闻或公司公告；标题不证明价格因果。")


def fetch_announcements(client, day, symbols, captured):
    rows, errors = [], {}
    start = (captured.date() - timedelta(days=90)).isoformat()
    for symbol in symbols:
        try:
            d = client.get("eastmoney", "https://np-anotice-stock.eastmoney.com/api/security/ann",
                {"ann_type": "A", "stock_list": symbol, "page_size": 20, "page_index": 1, "begin_time": start, "end_time": day})
            node = d.get("data")
            if d.get("success") != 1 or not isinstance(node, dict) or not isinstance(node.get("list"), list):
                raise FeedError("provider_no_result")
            for r in node["list"]:
                codes = [code(x.get("stock_code")) for x in r.get("codes", [])]
                published = timestamp(r.get("display_time"))
                notice_day = str(r.get("notice_date", ""))[:10]
                if symbol not in codes or published > captured or not start <= published.date().isoformat() <= day:
                    raise FeedError("wrong_source_identity")
                article = str(r.get("art_code", ""))
                if not re.fullmatch(r"AN\d{10,30}", article):
                    raise FeedError("invalid_article_id")
                rows.append({"code": symbol, "id": article, "title": clean(r.get("title"), 200),
                             "published_at": published.isoformat(), "notice_date": notice_day,
                             "categories": [clean(x.get("column_name"), 50) for x in r.get("columns", [])][:6],
                             "url": f"https://data.eastmoney.com/notices/detail/{symbol}/{article}.html"})
        except FeedError as exc:
            errors[symbol] = exc.code
    return result(rows, complete=False, errors=errors,
                  note="每只股票最近90天最多20条公告索引，含财报/业绩类公告；没有下载或核读正文，不等于已完成公告风险审查。")


FINANCIAL_FIELDS = {
    "income": ("INCOME", {"TOTAL_OPERATE_INCOME": "revenue_yuan", "PARENT_NETPROFIT": "parent_net_profit_yuan", "DEDUCT_PARENT_NETPROFIT": "adjusted_parent_profit_yuan"}),
    "balance": ("BALANCE", {"TOTAL_ASSETS": "assets_yuan", "TOTAL_LIABILITIES": "liabilities_yuan", "TOTAL_EQUITY": "equity_yuan"}),
    "cashflow": ("CASHFLOW", {"NETCASH_OPERATE": "operating_cashflow_yuan", "NETCASH_INVEST": "investing_cashflow_yuan", "NETCASH_FINANCE": "financing_cashflow_yuan"})}


def fetch_financials(client, day, symbols, captured):
    rows, errors = [], {}
    for symbol in symbols:
        for kind, (report, fields) in FINANCIAL_FIELDS.items():
            try:
                raw, _, _ = datacenter(client, "RPT_DMSK_FN_" + report,
                    f'(SECURITY_CODE="{symbol}")(NOTICE_DATE<=\'{day}\')', "REPORT_DATE", max_pages=1, page_size=4)
                accepted = []
                for r in raw:
                    disclosed, period = str(r.get("NOTICE_DATE", ""))[:10], str(r.get("REPORT_DATE", ""))[:10]
                    try:
                        date.fromisoformat(disclosed)
                        date.fromisoformat(period)
                    except ValueError:
                        raise FeedError("invalid_disclosure_date") from None
                    # No exact disclosure time: accept prior dates only, not midnight fabricated as publication.
                    if code(r.get("SECURITY_CODE")) != symbol or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", disclosed) or not period <= disclosed <= day:
                        raise FeedError("invalid_disclosure_date")
                    if disclosed == day:
                        continue
                    accepted.append({"code": symbol, "statement": kind, "report_date": period, "disclosure_date": disclosed,
                        "published_at": None, "values": {name: number(r.get(field)) for field, name in fields.items()}})
                if not accepted:
                    raise FeedError("no_prior_disclosure")
                rows.extend(accepted[:2])
            except FeedError as exc:
                errors[symbol + ":" + kind] = exc.code
    return result(rows, complete=False, errors=errors,
        note="各表最近两个可用报告期摘要，金额为接口原始元；利润/现金流按报告期累计，不是单季度。未验证重述版本；只作采集当日盘后研究，禁止用于过去的选股。披露当天且无准确时间的报表暂不采用。")


FETCHERS = {"dragon_tiger": fetch_dragon_tiger, "dragon_tiger_seats": fetch_seats, "sector_flow": fetch_sector,
            "themes": fetch_themes, "news": fetch_news, "announcements": fetch_announcements, "financials": fetch_financials}


def fetch(client, kind, day, symbols, captured):
    if kind in ("limit_up", "broken_limit_up", "limit_down"):
        return fetch_pool(client, kind, day)
    return FETCHERS[kind](client, day, symbols, captured)
