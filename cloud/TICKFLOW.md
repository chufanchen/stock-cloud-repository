# TickFlow：优先级、验证与竞价边界

## 已实现

| 数据用途 | 默认顺序 | 必须通过的检查 |
|---|---|---|
| 缺失历史日线 | TickFlow → 新浪 → 东方财富 | 不复权、标的/日期/量价合法、包含上一交易日、归档当天 OHLC 一致 |
| 当日收盘行情 | TickFlow → 腾讯 | 当天且北京时间15:00后时间戳、完整合法 OHLC/昨收/成交量 |
| 开盘集合竞价成交额 | 仍为有权限的 Tushare `stk_auction_o` | 日期、量额单位、成交价与当天开盘价一致 |

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

诊断失败示例：`missing_key` 表示工作流未取得Key；`http_401` 表示认证失败；`http_403` 表示无权限；
`http_429` 表示限流；`connection_error` / `timeout` 表示连接问题。
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

可行的后续方案是由常驻服务器采集并归档未来交易日的竞价证据：

1. 先向 TickFlow 确认：09:25撮合后至09:30连续竞价前，A股 `volume` / `amount` 是否只包含
   当天已成交的开盘集合竞价；单位分别是什么；时间戳代表哪一事件；是否存在延迟和昨日报价缓存。
   仅有 `session=pre_market` 或五档数据不能证明竞价已成交。
2. 获得确定语义后，服务器在北京时间09:25:05至09:29:50记录多个样本，仅针对前一天冻结候选。
   保存原始响应、服务商时间戳、服务器采集时间、市场代码、价格、量额和来源到私有R2。
   REST轮询遵守账户频率限制；若使用WebSocket，还需单独核对其权限。
3. 收盘任务只接收当天、窗口内、已完成撮合的数据，再检查成交价与开盘价、量额单位及一致性。
   遇到缺失/延迟/冲突即拒绝该候选模拟买入。若之后也取得Tushare历史接口权限，可用来交叉核验。

这部分采集器与竞价归档接入尚未启用，需先确认供应商语义并配置实际服务器。
它与现有架构兼容：服务器采集 → R2留存 → Actions收盘结算/AI复盘 → Pages展示。
不适合用普通GitHub定时任务保证09:25这一窄窗口；它也不能在收盘后补造从未保存的历史竞价证据。
尚未完成上述验证期间，可继续补行情、选股与AI复盘，缺可靠竞价数据的候选不模拟买入。
如想改用其他流动性指标，应建立单独策略版本和回测，不能混入当前收益记录。

## 官方依据

- [认证与速率限制](https://docs.tickflow.org/zh-Hans/api-reference/introduction)
- [完整/免费服务范围](https://docs.tickflow.org/zh-Hans/quickstart)
- [默认复权方式、免费服务限制](https://docs.tickflow.org/zh-Hans/faq)
- [批量接口与A股成交量单位示例](https://docs.tickflow.org/zh-Hans/sdk/python-examples)
- [OpenAPI接口与字段定义](https://docs.tickflow.org/zh-Hans/api-reference/openapi.json)
- [WebSocket功能权限与缓存快照说明](https://docs.tickflow.org/zh-Hans/api-reference/websocket)
