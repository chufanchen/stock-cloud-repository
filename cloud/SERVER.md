# 用自己的服务器运行

推荐部署为：**服务器承担每天运行，GitHub保存代码与执行测试，Actions保留手动补跑，R2保存权威状态与研究记忆。** 服务器仍通过通用模型接口调用AI，无需本地GPU。

展示层由 [GitHub Pages](PAGES.md) 承担，GitHub在21:17、22:17、23:17读取最新结果发布网站；服务器无需部署网页服务，也无需向GitHub推送账本文件。

这里提供Linux + systemd配置模板，需在实际服务器上安装并核对网络、凭据与运行结果。仅修改仓库变量不会部署服务器。也可以沿用Actions执行每日流程，两者使用同一套代码。

## 1. 准备代码和环境

先按 [项目README](../README.md) 配置密钥，并确认R2已初始化。已有R2时直接恢复，不要重建；新会议目录会自动加入现有状态。

从Mac导出干净代码目录，再传到服务器 `/opt/wuming`，或从当前代码仓库检出到该路径。不要复制Mac的 `.venv` 或原始五个席位的大数据库。

服务器需要可执行的Python 3.14、网络访问R2/行情/模型服务。以下示例假设Python 3.14已安装在系统可用路径，且你有sudo权限：

```bash
sudo useradd --system --user-group --home-dir /var/lib/wuming --no-create-home wuming
sudo install -d -o wuming -g wuming -m 700 /var/lib/wuming
sudo python3.14 -m venv /opt/wuming/.venv
sudo /opt/wuming/.venv/bin/python -m pip install -r /opt/wuming/cloud/requirements.lock
sudo install -d -m 700 /etc/wuming
if ! sudo test -f /etc/wuming/cloud.env; then
  sudo install -m 600 /opt/wuming/cloud/.env.example /etc/wuming/cloud.env
fi
sudoedit /etc/wuming/cloud.env
```

如果用户或配置文件已存在，跳过对应创建步骤；不要用模板覆盖已填写的密钥。服务以 `wuming` 普通用户运行，代码和虚拟环境需可读/可执行。不要把Python放在个人家目录内，服务模板限制了家目录访问。

环境文件填写：`R2_ENDPOINT_URL`、`R2_BUCKET`、`R2_PREFIX`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、`AI_BASE_URL`、`AI_MODEL`、`AI_API_KEY`，以及 TickFlow 首选源的 `TICKFLOW_API_KEY`（可选 `TICKFLOW_BASE_URL`）和可选的 `TUSHARE_TOKEN`。字段与Actions相同，无需新增会议专用密钥。文件使用 `KEY=value`，不加 `export`，也不要依赖 `$变量` 展开。

## 2. 先选定日常执行端

在GitHub仓库的 **Settings → Secrets and variables → Actions → Variables** 新增：

```text
DAILY_EXECUTOR = server
```

这样每天的Actions任务会跳过，手动 `Run workflow` 和代码测试仍可用。若选择Actions主运行，删除该变量或设置 `github`，并保持服务器timer关闭。未设置时默认保留原Actions定时行为。

不要同时把服务器和Actions都设为自动主运行。服务器有本地进程锁，R2也会拒绝并发旧状态提交，但这不能避免重复API费用。

## 3. 安装定时配置并手动检查

```bash
sudo install -m 644 /opt/wuming/cloud/deploy/wuming-daily.service /etc/systemd/system/
sudo install -m 644 /opt/wuming/cloud/deploy/wuming-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start wuming-daily.service
sudo journalctl -u wuming-daily.service -n 100 --no-pager
```

首次手动检查安排在交易日北京时间20:00后；休市日会正常跳过，不能因此证明行情和模型接口已验证。确认日志中的结算和六个会议环节完成后，再启用定时：

```bash
sudo systemctl enable --now wuming-daily.timer
systemctl list-timers wuming-daily.timer
```

模板明确使用 `Asia/Shanghai`，每周一至周五20:37运行、21:37重试，程序再检查A股交易日历。timer不自动补执行关机期间错过的时间，避免重启时用今天数据冒充历史。已有归档可手动补跑；漏行情日仍须按主部署指南处理。

同一service尚未结束时，systemd不会再启动一份。单次上限55分钟；各席成功结果逐步保存到R2，超时可在下一次恢复。定时行为参考 [systemd.timer官方文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)。

## 4. 查看结果和恢复

每次运行创建独立目录：

```text
/var/lib/wuming/runs/<时间-随机编号>/
  run.json                 运行状态与开始/结束时间
  state/reports/           日报
  state/meeting/           完整讨论和逐日记忆
  state/research/          累计研究提案
```

每次从R2读取最新版本，避免复用旧账本。成功后只删除该次临时恢复的 `market.db` 副本，报告及元数据保留；数据库已在R2。失败目录完整保留便于排查。长期运行要定期清理已确认不需要的旧运行目录；R2历史版本保留策略沿用主指南。

手动重跑今天：`sudo systemctl start wuming-daily.service`。补某个旧日期的会议，可以先停止timer并确认service已结束，再临时运行：

```bash
sudo systemctl stop wuming-daily.timer
sudo systemd-run --unit=wuming-review --wait --collect \
  --property=User=wuming --property=Group=wuming \
  --property=WorkingDirectory=/opt/wuming \
  --property=EnvironmentFile=/etc/wuming/cloud.env \
  --property=UMask=0077 \
  --setenv=PYTHONPATH=/opt/wuming/cloud --setenv=TZ=Asia/Shanghai \
  /opt/wuming/.venv/bin/python -m ashare_cloud.runner \
  --work-dir /var/lib/wuming --command review --date 2026-10-26
sudo systemctl start wuming-daily.timer
```

替换为实际已结算日期。服务器不可用时，也可在Actions手动选择 `mode=review` 和日期，读取同一个R2前缀继续。恢复成功后再恢复单一主调度端。

如果以后要把结算和AI研究分开调度，可先 `run --skip-ai` 保存结算，再 `review --date ...` 只做会议；两个任务按顺序执行并使用同一R2前缀。本次默认保持一个完整任务，部署更简单。

当前未配置服务器故障推送：失败会记录在systemd状态、journal和 `run.json`。可接入你已有的服务器监控；没有代你发送任何通知或配置消息服务。

## 5. 可选：只用服务器采集早盘竞价

没有 Tushare 竞价权限时，可安装 `wuming-auction.service` / `wuming-auction.timer`，北京时间09:24:50启动腾讯免Key采样。证据单独存入R2，晚间自动读取。无需改变晚间执行端，`DAILY_EXECUTOR=github` 时仍可启用服务器竞价采集。

详细安装命令、同日冻结计划要求、金额推定口径和首次早盘验证步骤见 [竞价采集与回退](AUCTION.md)。模板未自动部署；每个账户只启用一个采集端。
