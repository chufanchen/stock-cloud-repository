# 本地持久运行与服务器部署

主状态是 `<work-dir>/state/`。每次运行直接复用本地数据库与账本，R2 不再是日常启动时的状态来源。默认 Mac 工作目录为项目下 `.local-runtime/`。此文取代旧的“每次从R2下载、成功后删除数据库”部署说明。

## 迁移步骤

1. 停止其他设备的定时任务。新版 GitHub `daily.yml` 已取消定时计算，只保留迁移说明。
2. 配置根目录 `.cloud.env`，不要修改旧账本。部署代码可从 GitHub 获取；Python3.14 与依赖见根 README。
3. 使用 `desktop task --command restore` 从旧 R2 导入一次，目标必须不存在。或 `--command import --source /已有状态目录` 从本地一致副本导入。
4. `desktop task --command backtest` 核验已归档结算；`--command publish` 备份并发布现有结果。步骤失败时检查原因，不重置账本。
5. Mac 用 `desktop install` 安装当前用户 LaunchAgent。运行日期与时间统一以 Asia/Shanghai 判断；电脑休眠期间不会采集，不伪造漏采数据。

状态请直接读取 `.local-runtime/last-run.json`，账户只读检查使用：

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.cli status --state-dir .local-runtime/state --local
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command run
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command review --date 2026-10-27
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command sync
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command backtest
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command publish
```

`run` 只允许收盘后；补历史日必须有真实归档。缺冻结计划或中间未结算日会阻止后续成交，迁移不绕过这些约束。`review` 仅处理已经结算的日期。首次恢复后要检查 `last_settled` 与 `plans`，确认是否存在历史缺口。

`--offline` 只关闭 R2 备份/发布；run/review 仍可能调用数据或模型接口，不表示完全断网。纯离线核验用 `--command backtest --offline`。

## 数据与备份

```text
.local-runtime/
  state/market.db             持久行情库，增量更新
  state/account.json          模拟账本与冻结计划
  state/snapshots/            精确日期行情归档
  state/auctions/             实际采集竞价证据
  state/meeting/              会议与记忆
  state/research/             研究数据与待验证提案
  state/backtests/            重放结果与输入哈希
  runs/<编号>/run.json        每次任务记录
  last-run.json               最近任务、备份版本和发布状态
  schedule.json               定时窗口执行记录
  logs/                      Mac 调度日志
```

R2 原 `<prefix>/HEAD.json` 保留为旧迁移来源，新运行不写它。

- `<prefix>/backups/checkpoints/`：轻量私有进度包，行情或会议阶段保存；不含数据库，不能单独作为完整恢复点。
- `<prefix>/backups/HEAD.json`：最近完整备份。
- `<prefix>/backups/versions/<编号>.json`：指定版本的完整备份清单。
- `<prefix>/backups/databases/`：SQLite backup API 生成一致副本后压缩，包含已提交 WAL；相同哈希复用。每次任务结束或失败均尝试完整备份，增量只发生在本地采集，不宣称对象存储上传是块级增量。
- `<prefix>/publications/`：公开字段结果包，不含完整账户、数据库、模型输入或私有记忆。可用独立 R2_PUBLIC_BUCKET 隔离权限。

本地进度先写盘，备份失败不撤销结算；命令仍以失败退出提醒云端不完整，使用 publish 重试即可，不需要重新结算。不要同时在两台设备运行同一账户，本地锁只保护一台主机。完整备份的远端 HEAD 有条件更新，但不能代替跨机器主运行端管理。

恢复到新目录：

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command restore \
  --from-backup --work-dir .local-runtime-recovered \
  --version versions/实际32位编号.json
```

不传 version 读取最近完整备份。先比对账户、哈希链、数据库和最后结算日期，再停掉原调度并切换目录。禁止把旧备份直接覆盖正在运行的状态。历史备份暂不自动删除；设置保留期前确认被保留清单引用的数据库和状态包仍存在，不能直接对所有 databases 对象按年龄删除。

## Mac 定时与停用

安装器生成 `~/Library/LaunchAgents/org.wuming.local-daily.plist`，登录后每15秒检查北京时间窗口。日常任务20:37与21:37各保留5分钟启动窗口；竞价09:24:50至09:25:50启动。休市跳过。失败不会在同一窗口无限重试。机器唤醒发生在窗口外时不追补。

```bash
launchctl print gui/$(id -u)/org.wuming.local-daily
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/org.wuming.local-daily.plist
```

Mac 对 Documents 的访问可能受系统隐私设置限制；出现 Operation not permitted 时检查日志和终端/Python文件访问权限。安装器不会修改系统睡眠设置。当前无额外消息通知服务。

## Linux

把干净代码放 `/opt/wuming`，Python环境放 `/opt/wuming/.venv`，凭据 `/etc/wuming/cloud.env` 权限600，工作目录 `/var/lib/wuming` 所属专用 wuming 用户。恢复、核验并发布一次后，安装 `cloud/deploy/wuming-daily.*` 与 `wuming-auction.*`。

service 中的 runner 固定复用 `/var/lib/wuming/state` 并带 `--cloud`。模板仍由 systemd 设置环境文件；不要把 Mac 虚拟环境拷到 Linux。回测可独立执行：

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.runner --work-dir /var/lib/wuming --command backtest --cloud
```

切换到服务器前卸载 Mac 定时任务，使用最新完整备份迁移；从旧顶层 HEAD 恢复会遗漏新架构期间的进度。

## 新观察期

仅在用户明确确认后使用 `local_state.start_observation`：从已校验空仓账户承接现金到新目录，并记录原账本哈希和最后结算日。旧目录只读保留；不迁移未完成会议或拼接收益。`.local-active.json` 可记录 `work_dir`，供 Mac desktop 默认选择活动目录。首次运行在开始日前一交易日收盘后只冻结首日计划；开始日若计划缺失，停止，不补造。
