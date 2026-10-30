# 无名游资 A 股模拟交易与 AI 研究

**本地 / 自有服务器计算，GitHub Pages 展示，R2 备份。** 日常数据保存在固定目录并增量更新。多角色 AI 通过通用兼容接口讨论，交易规则不会被 AI 自动修改；没有真实券商下单功能。

## 运行结构

```text
数据接口 → 本地持久行情库 → 固定规则选股 / 冻结计划 / 模拟结算
                           ├─ 多角色会议 → 共享记忆、待验证提案
                           ├─ 归档行情 + 冻结计划 → 离线重放核验
                           ├─ R2 私有备份（完整恢复点、轻量进度包）
                           └─ 公开字段结果包 → R2 → GitHub Actions → Pages
```

Mac 休眠或关机会错过采集窗口。需要全天无人值守时，将同一套持久化运行器迁移到 Linux 服务器。两台机器不能同时作为同一账户的主运行端。GitHub 不再定时执行交易或 AI，也不下载行情数据库。

## 代码结构

| 位置 | 用途 |
|---|---|
| `cloud/ashare_cloud/runner.py` | 本地任务入口、单实例锁、任务状态、备份和发布 |
| `cloud/ashare_cloud/local_state.py` | 只向新目录迁移、SQLite 一致备份、指定版本恢复 |
| `cloud/ashare_cloud/desktop.py` | Mac 配置加载、北京时间调度、LaunchAgent 安装 |
| `cloud/ashare_cloud/cli.py` | 收盘结算、冻结下一日计划、复盘续跑；兼容旧 R2 命令 |
| `cloud/ashare_cloud/market.py` / `sources.py` | 行情与历史数据、覆盖率、限速及断点 |
| `cloud/ashare_cloud/auction.py` | 腾讯免 Key 竞价快照采集与验证 |
| `cloud/ashare_cloud/engine.py` | 固定规则的模拟成交、T+1、费用、拒绝原因 |
| `cloud/ashare_cloud/research_data.py` / `research_sources.py` | 龙虎榜、涨跌停、财报等研究资料及来源状态 |
| `cloud/ashare_cloud/meeting.py` / `review.py` | 多角色会议、审计、主持总结、逐阶段保存、日报 |
| `cloud/ashare_cloud/backtest.py` | 冻结计划与归档行情的离线重放；不反向生成选股信号 |
| `cloud/ashare_cloud/publication.py` / `pages.py` | 公开字段白名单、轻量结果包、静态网页 |
| `cloud/config.json` / `meeting.json` / `research_data.json` | 固定策略、角色模型额度、研究数据配置 |
| `cloud/tests/` | 成交、状态恢复、数据源、AI 续跑与导出测试 |
| `cloud/deploy/` | Linux systemd 模板 |
| `.github/workflows/` | 代码测试、Pages 发布、手动数据接口检查 |

本地原有利弗莫尔、巴菲特、科斯托拉尼、最强 AI、AI 进化初始版等目录是历史脚本、研究和原始资料；角色提示与部分方法已整理到 cloud，原目录的全部策略与回测没有一并迁移。公开仓库只包含可部署代码，不包含这些大数据库、密钥或私人账本。

## 本机快速部署（已有 R2 账户）

在项目根目录执行；Mac 可沿用已安装的 `.venv`：

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r cloud/requirements.lock
cp -n cloud/.env.example .cloud.env
chmod 600 .cloud.env
```

编辑 `.cloud.env` 后恢复一次。**已有 `.local-runtime/state` 时拒绝覆盖。**

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command restore
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command backtest
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command publish
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop install
```

恢复默认读取原 R2 前缀的旧账户；以后恢复新备份用 `--from-backup`，必须指定新的 `--work-dir`。新账户需要先核对原始空仓账本再用 `cli init` 初始化，详见 [部署与恢复](cloud/SERVER.md)。不要对现有账户重新初始化。

## 密钥放哪里

本地 `.cloud.env`：

- `AI_BASE_URL`、`AI_MODEL`、`AI_API_KEY`：兼容接口，多角色共用。默认分析额度 6000、审计 12000、主持 16000；具体按模型能力调整。
- `R2_ENDPOINT_URL`、`R2_BUCKET`、`R2_PREFIX`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`：私有备份与结果上传。
- `R2_PUBLIC_BUCKET`：可选独立结果桶；为空时沿用私有桶。桶仍可保持私有，由 Actions 读取。
- `TICKFLOW_API_KEY`：可选首选行情源；没有权限或返回 403 时走已配置回退。
- `TUSHARE_TOKEN`：可选且需要对应权限；不具备竞价权限时使用实际早盘腾讯快照，无法事后补采。

GitHub 仅需 R2 端点、结果桶、前缀及只读凭据。推荐独立结果桶，并设置 `R2_PAGES_ACCESS_KEY_ID` / `R2_PAGES_SECRET_ACCESS_KEY` 为该桶只读；同桶方案的只读令牌可能仍可读私有备份，不能把程序路径白名单当作权限隔离。旧 AI / 行情 Secrets 不再被每日计算工作流使用。

## 每日流程（北京时间）

- 09:24:50 起：采集当天冻结候选的竞价证据，验证窗口 09:25:05–09:29:50。
- 20:37：增量行情 → 模拟结算 → 下一交易日冻结预案 → 多角色会议 → 备份、发布结果包。
- 21:37：重试；已结算日不重复成交，已成功会议阶段不重复调用。
- 22:20：归档重放核验并发布回测结果。
- GitHub Pages 在 09:17、21:17、22:17、23:17 定时读取结果包，也可手动运行“发布复盘网页”。调度可能延迟。

Mac 调度每 15 秒检查北京时间窗口，窗口外不会补执行错过的历史任务。日志在 `.local-runtime/logs/`，最近任务在 `.local-runtime/last-run.json`。不自动唤醒电脑，不自动补造缺失计划。

## 功能边界

网页有每日总览、历史日报、AI 会议室、研究提案、早盘预案、回测结果。预案展示真实冻结候选与固定执行条件，不保证买入。回测目前是归档重放与逐日结算核验，**不是**多年全市场策略收益验证；没有数据、冻结计划缺失或结果不一致均明确失败。新研究提案仍需人工确定实验设计与数据，不能自动改策略。

缺失财报、龙虎榜、竞价、覆盖率不足、模型失败会明确记录；数据缺失不能当作零。已有会议继续使用冻结的输入和配置，不因升级改变历史判断。

## 其他文档

- [本地 / Linux 部署与恢复](cloud/SERVER.md)
- [网页发布与字段边界](cloud/PAGES.md)
- [数据来源](cloud/DATA_SOURCES.md)、[研究数据](cloud/RESEARCH_DATA.md)、[TickFlow](cloud/TICKFLOW.md)
- [竞价采集](cloud/AUCTION.md)、[多角色会议](cloud/MEETING.md)

验证：`PYTHONPATH=cloud .venv/bin/python -m pytest cloud/tests -q`。不要将 `.cloud.env`、`.local-runtime`、历史数据库提交到 GitHub。

已切换观察期时，`desktop` 自动读取本机 `.local-active.json` 中的工作目录；旧目录保留只读，不拼接两期收益。新观察期前一交易日晚间 run 仅准备首日冻结计划，不生成提前结算。
