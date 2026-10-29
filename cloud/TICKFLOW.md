# TickFlow：优先级、验证与竞价边界

## 已实现

| 数据用途 | 默认顺序 | 必须通过的检查 |
|---|---|---|
| 缺失历史日线 | TickFlow → 新浪 → 东方财富 | 不复权、标的/日期/量价合法、包含上一交易日、归档当天 OHLC 一致 |
| 当日收盘行情 | TickFlow → 腾讯 | 当天且北京时间15:00后时间戳、完整合法 OHLC/昨收/成交量 |
| 开盘集合竞价成交额 | Tushare `stk_auction_o` → 腾讯早盘归档快照 | 专用接口优先；快照需同日窗口、双样本、量价和冻结计划校验 |

优先级在 `cloud/config.json` 的 `history_sources` 和 `quote_sources` 中调整。
移除一个来源即可停用，列表不能留空、重复或填入未实现的来源。
没有 TickFlow Key 时跳过它，其他来源仍可运行。配置 Key 后自动成为首选。
新接入未改变策略版本、资金参数、冻结计划要求或97%完整性门槛。

历史请求共用40分钟预算和最小请求间隔；TickFlow 每批最多默认50只，避免逐只请求。
来源被401/403拒绝时本轮停用；429遵守该来源的冷却期；网络故障、坏数据和缺失项按优先级回退。
TickFlow 连续3次真实网络请求失败会熔断到本轮结束，不会因同一批中3只股票缺数据而停用服务。
不通过改IP、轮换同服务入口来继续访问被拒绝的接口。
归档数据库只补缺行；股票实际来源与回退原因写入诊断，便于复核。

## GitHub Actions 配置与检查

1. 仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret**。
   添加 `TICKFLOW_API_KEY`，值为你自己的 Key。
2. `TICKFLOW_BASE_URL` 是可选 **Variable**，默认 `https://api.tickflow.org`。
   若服务商要求指定区域，请按其文档填写 HTTPS 根地址，不加 `/v1`，不要在地址中嵌入密钥。
3. **Actions → 行情数据源检查 → Run workflow → main**。
   这个任务只读取沪、深、北样本日线和行情，以及上证指数；不读取R2、不结算、不调用AI。
   输出最新日线日期、行情时间及检查结果。通过仅表示样本接口/字段检查通过，不代表全市场数据齐全。
4. 样本检查通过后，在日常流程查看 `diagnostics/quotes-日期.json` 和 `history-日期.json`。
   `selected_counts` / `selected_sources` 表示实际使用的来源；回退可成功完成，不能只凭绿色任务断定 TickFlow 已生效。

诊断失败示例：`missing_key` 表示工作流未取得Key；`http_401` 表示认证失败；`http_403` 表示权限或访问被拒绝；
`http_429` 表示限流；`connection_error` / `timeout` 表示连接问题。
检查会输出失败阶段、接口路径、响应格式和固定的 `response_hint` 分类，以区分API权限提示与网页网关拦截。
分类仅来自响应特征，不是已经证实的根因；不会输出原始错误文案、服务商返回的凭据或任意错误码。
配置在 Secrets 页面修改，不要把 Key 放进仓库或日志。代码不追随携带密钥的重定向。

本机在项目根目录 `.cloud.env` 加相同字段；服务器在 `/etc/wuming/cloud.env` 加相同字段。
确保环境变量已加载后，运行 `PYTHONPATH=cloud python -m ashare_cloud.source_check`。
本次代码验证使用模拟响应；真实付费Key尚未提供给运行环境，仍须完成上面的在线检查。
免费日线服务存在，但本适配器当前使用有Key的完整服务，不把免费端点当作被限流后的绕行入口。

## 没有 Tushare 竞价权限时

TickFlow 官方公开规范列出了普通实时行情、五档盘口、日线和分钟线；目前没有发现单独定义的
“开盘集合竞价成交额”接口。`quotes.amount` 为普通行情成交额字段，文档没有承诺在09:25后它就是
已完成开盘集合竞价的成交额。因此本次没有把它自动填入 `auction_amount`。
第一分钟成交额、全天成交额、五档挂单量也不能直接替代开盘竞价实际成交额。

现在已提供独立的**腾讯免Key早盘快照回退**，参考原 `watch_auction.py`，详见 [采集与部署说明](AUCTION.md)。该适配器不使用 TickFlow 普通行情金额。

本机或服务器在北京时间09:25:05至09:29:50采集同日冻结候选，保留原始字段和两个时间戳。通过双样本稳定性、价格和量额单位检查后，保守推定竞价额并存入私有R2；晚间Actions或服务器读取。没有证据的股票仍拒绝模拟买入。

腾讯方案是带校验的快照推定，尚未完成真实早盘样本验证，定时器也需自行部署。TickFlow竞价适配仍需确认其成交量额和时间戳语义，不能因腾讯适配器存在就自动套用。任何采集方案均无法在收盘后补造从未保存的历史竞价证据。

## 官方依据

- [认证与速率限制](https://docs.tickflow.org/zh-Hans/api-reference/introduction)
- [完整/免费服务范围](https://docs.tickflow.org/zh-Hans/quickstart)
- [默认复权方式、免费服务限制](https://docs.tickflow.org/zh-Hans/faq)
- [批量接口与A股成交量单位示例](https://docs.tickflow.org/zh-Hans/sdk/python-examples)
- [OpenAPI接口与字段定义](https://docs.tickflow.org/zh-Hans/api-reference/openapi.json)
- [WebSocket功能权限与缓存快照说明](https://docs.tickflow.org/zh-Hans/api-reference/websocket)
