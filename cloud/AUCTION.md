# 没有 Tushare 竞价权限：腾讯早盘快照回退

参考原始 `AI进化初始版/skill-premarket-3pick-plan/scripts/watch_auction.py`，新增 `ashare_cloud.auction`。原脚本保持不变。腾讯公开行情不需要 API Key，仍需联网访问，服务可用性及字段语义不受本项目保证。

默认竞价来源顺序：

```json
"auction_sources": ["tushare", "tencent_snapshot"]
```

在 `cloud/config.json` 修改这一字段即可。没有 Tushare Token、无权限、接口错误、个别股票缺失或开盘价不匹配时，逐项尝试早盘快照。仅需快照时可设为 `["tencent_snapshot"]`；停用快照可设为 `["tushare"]`。没有有效证据时跳过相应模拟买入，原有 R0 的5%限制保持不变。

## 为什么不直接运行旧脚本

旧脚本读取了成交额 `amount`，但写入文件时遗漏了它，也未保存行情源时间戳和开盘价。它使用机器本地时区，北交所 `92`/`4` 开头股票的映射不完整，并在最后才写盘。

新版本用于留下可审查的开盘后证据，不采集虚拟报价轨迹：

- 全程使用北京时间。允许09:24至09:29:50启动；默认timer为09:24:50。
- 真正取样只在09:25:05至09:29:50。回包晚于窗口也丢弃，09:30后不再取数。
- 只采当天已有冻结计划中的候选；不会重选、补造计划或改变规则。
- 保存原始字段字符串，包括行情源时间戳、采集时间、开盘价、现价、昨收、成交量和成交额。买卖一档仅留作审计，不参与 R0。
- 至少两个相隔2至30秒的样本量额、价格一致，源时间戳必须是同日09:25至09:30前，且不晚于采集时间。
- 现价必须等于开盘价；昨收必须匹配冻结计划。收盘使用时还要匹配当天归档开盘价。
- 量按手转换为股，成交额按万元转换为元；比较 `成交量 × 100 × 开盘价` 与 `成交额 × 10000`，只允许100元或一手金额以内的显示舍入差。用于模拟的金额保守取两者较小值。
- 401/403/429立即停止此轮采集；连续3次网络失败停止。没有换IP或换同服务入口重试。

**口径是“早盘快照推定竞价额”，不是交易所专用接口返回的精确竞价成交额。** 推定依赖腾讯在撮合后更新当日累计成交量额这一假设。双样本、时间戳及量价校验可以排除明显异常，不能证明供应商内部字段永远没有缓存或变更。首次上线需在真实早盘核对样本；代码测试及盘中 `--probe` 不能替代这项核验。有 Tushare 时继续优先使用它。

时间窗口依据：[上交所交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)、[深交所交易机制说明](https://investor.szse.cn/institute/rules/t20230629_601434.html)。交易所规则说明竞价与连续竞价时段，并不为腾讯字段定义背书。

## 先做只读连接检查

在项目根目录、使用已安装依赖的环境运行：

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.auction --probe
```

随时可运行，不需要密钥，不读取账户，不保存可供成交使用的证据；显示沪深北三个样本的源时间戳及字段是否齐全。检查通过仅说明样本接口可访问，仍需要早盘采集。

## 与 GitHub Actions + R2 配合

早盘由本机或服务器采集 → 证据保存到私有 R2 → 晚间 Actions/服务器恢复对应证据 → 按原规则结算、复盘 → Pages发布结果。

只需已有 R2 连接字段及读写权限，不新增付费行情密钥。证据路径为：

```text
<R2_PREFIX>/auction-captures/YYYY-MM-DD/<冻结计划哈希>.json
```

采集器只读取较小的账户状态包，不下载行情数据库，不更新 `HEAD.json`、账户或交易计划。上传使用“仅不存在时写入”，同日同计划首份有效采样包不覆盖。没有合格样本不上传，部分成功只允许有证据的股票继续；后续重复上传会报错，应先检查首份记录。晚间任务把证据保存为 `auctions/YYYY-MM-DD.json`，随正常状态归档；Pages不会发布原始快照。

在交易日北京时间09:24:50左右，加载你自己的 `.cloud.env` 后运行：

```bash
set -a
source .cloud.env
set +a
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.auction
```

本地副本保存于 `.server-runs/auction/`。同一天文件已存在时拒绝覆盖，先检查并归档旧文件再决定是否重试。关机、休眠、网络故障或错过窗口不能在晚上补采。普通 Actions 定时任务不保证准点，因此不增加早盘 Actions cron。

### Linux 服务器定时

先完成 [服务器基础部署](SERVER.md)，确保系统时钟同步、R2环境文件和用户权限正确，再安装：

```bash
sudo install -m 644 /opt/wuming/cloud/deploy/wuming-auction.service /etc/systemd/system/
sudo install -m 644 /opt/wuming/cloud/deploy/wuming-auction.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wuming-auction.timer
systemctl list-timers wuming-auction.timer
```

timer每周一至五北京时间09:24:50启动，程序过滤休市日；`Persistent=false`，错过后不会补执行。使用 `/var/lib/wuming/auction/` 保存记录，日志查看：

```bash
sudo journalctl -u wuming-auction.service -n 100 --no-pager
```

这只负责早盘采样，与晚间计算端选择独立：可以保持 `DAILY_EXECUTOR=github`，仅服务器启用竞价timer；也可让服务器承担两者。每个账户只启用一个竞价采集端。

本项目只提供配置模板，尚未安装到你的服务器或在本机启用定时。

## 完全本地运行

先恢复一份含当天有效冻结计划的账户，再在早盘窗口运行：

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.auction --state-dir .cloud-state-local
```

此模式只读写本地，证据位于 `.cloud-state-local/auctions/`。晚间在同一副本使用 `cli run --local --state-dir .cloud-state-local`，就能读取证据。不要把本地模式与默认 R2 模式混淆。

无当天冻结计划会报错；候选为空正常结束且不访问腾讯。当前历史旧计划缺失的问题仍须单独处理，本采集器不会越过计划冻结限制。旧 `watch_auction.py` 保存的轨迹缺少关键证据字段，不能直接导入为成交依据。

## 验收

1. `--probe` 三个样本均返回字段和时间戳。
2. 实际早盘采集返回 `complete`，或明确列出 `partial` 的缺失股票。
3. 检查同日源时间戳、两个采样时间、开盘价与量额；必要时与有权限的竞价数据交叉核对。
4. 晚间 `snapshots/日期.json` 的 `auction_note` 写明快照来源，相关股票的 `auction_source` 为 `tencent_opening_snapshot_v1`。
5. 缺失、价格冲突、旧日期、连续竞价后数据均拒绝模拟买入，不补成0、不填估计的全天成交额。

本次测试覆盖模拟数据中的单位、日期、双样本、R2隔离写入、权限失败回退及R0衔接；真实普通报价样本可达。真实早盘采集尚未验证，不能据此宣称整条竞价链路已上线。
