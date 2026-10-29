# 云端运行模块

`cloud/` 是当前自动选股、模拟结算、AI复盘和网页发布的实现目录。**完整代码结构、密钥清单与部署步骤已集中到 [项目根 README](../README.md)**，避免多份部署说明互相矛盾。

## 从哪里开始

| 你的情况 | 阅读位置 |
|---|---|
| 第一次部署完整系统 | 根README第5—9节：准备环境、初始化R2、选择执行端、发布Pages |
| 已有R2，只换机器或更新代码 | 根README第6节“已经有R2账户状态”；无需重新初始化 |
| 使用GitHub执行每日流程 | 根README第7节；`DAILY_EXECUTOR=github` |
| 使用自己的服务器 | 根README第8节及 [SERVER.md](SERVER.md)；`DAILY_EXECUTOR=server` |
| 只设置网页展示 | [PAGES.md](PAGES.md) |
| 配置TickFlow或排查403 | [TICKFLOW.md](TICKFLOW.md) |
| 了解AI如何共享记忆 | [MEETING.md](MEETING.md) |
| 补跑、恢复或检查结果 | 根README第10—12节 |

## 配置与入口

- [config.json](config.json)：模拟交易参数、行情来源优先级、覆盖率和抓取预算。
- [meeting.json](meeting.json)：角色职责、模型覆盖、输出额度和会议开关。
- [.env.example](.env.example)：R2、模型和行情服务的环境变量模板。
- [ashare_cloud/cli.py](ashare_cloud/cli.py)：`init`、`bootstrap`、`run`、`review`、`sync`、`status`、`restore`。
- [ashare_cloud/runner.py](ashare_cloud/runner.py)：服务器单实例执行入口，每次恢复到新的目录。
- [ashare_cloud/source_check.py](ashare_cloud/source_check.py)：TickFlow样本检查，不访问账户或AI。
- [ashare_cloud/pages.py](ashare_cloud/pages.py)：从R2或本地状态生成静态网站。

在项目根目录运行命令，并设置 `PYTHONPATH=cloud`。本机CLI不会自动读取 `.cloud.env`；需先加载环境变量。

`run/review/sync` 默认恢复并保存R2状态，恢复目录必须为空；`--local` 使用已有本地状态、不读写R2。
`review` 只补已结算日的会议；`sync` 只续补已归档收盘快照对应的历史行情；两者不能补造缺失的盘前计划。

## 功能边界

R2是权威状态，Pages是只读展示。AI角色可以积累研究记忆和提出待验证提案，但不会自动改交易规则或账本。

当前没有已验证的TickFlow竞价采集器。没有可靠开盘竞价成交额的候选不模拟买入，日常选股和复盘仍可继续。
真实接口权限与当日数据质量需单独检查，不能用代码测试通过来代替。
