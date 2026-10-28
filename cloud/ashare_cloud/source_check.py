"""Read-only TickFlow contract check. Does not access R2, the ledger or any AI API."""
from __future__ import annotations

import json

from .common import now
from .engine import validate_bar
from .history import HistoryPaused, Pacer
from .tickflow import SourceError, TickFlow


def main():
    symbols = ["sh600000", "sz000001", "bj920002"]
    stage = "配置"
    client = None
    try:
        client = TickFlow(Pacer(interval=1, budget=120))
        stage = "历史日线"
        print("开始检查：历史日线 /v1/klines/batch", flush=True)
        client.prepare([(s, 10) for s in symbols])
        histories = {s: client.history(s, 10) for s in symbols}
        stage = "实时行情"
        print("开始检查：实时行情 /v1/quotes", flush=True)
        quotes = client.quotes(symbols + ["sh000001"])
        for symbol in symbols + ["sh000001"]:
            if symbol not in quotes:
                raise SourceError("missing_or_invalid_quote")
            b = quotes[symbol]
            validate_bar(b, b["date"])
            if b["date"] > now().date().isoformat():
                raise SourceError("future_quote")
        checks = []
        for symbol, rows in histories.items():
            b = quotes[symbol]
            last = rows[-1]
            if last[1] == b["date"] and b["timestamp"][8:12] >= "1500":
                if any(abs(last[i + 2] - b[key]) > 0.011
                       for i, key in enumerate(("open", "close", "high", "low"))):
                    raise SourceError("daily_quote_price_mismatch")
            checks.append({"symbol": symbol, "daily_rows": len(rows), "latest_daily": last[1],
                           "quote_timestamp_beijing": b["timestamp"]})
        print(json.dumps({"status": "passed", "checks": checks,
                          "note": "仅接口与字段检查；日期见上，不代表全市场覆盖通过或竞价数据可用"},
                         ensure_ascii=False, indent=2))
        return 0
    except (SourceError, HistoryPaused) as exc:
        print(f"数据源检查未通过：{stage} {exc.reason}")
        print(json.dumps({"failed_stage": stage,
                          "diagnostic": getattr(exc, "diagnostic", {}),
                          "network": client.statistics() if client else {}}, ensure_ascii=False, indent=2))
        print("response_hint 是响应特征分类，不是已证实的根因；未输出服务商原文、密钥或请求头。")
        return 1
    except ValueError:
        print("数据源检查未通过：配置或行情字段不合格")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
