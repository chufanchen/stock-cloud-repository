# 每日选股、模拟结算和 AI 复盘：部署指南

本改造支持 **自己的服务器或 GitHub Actions 执行 Python，Cloudflare R2 保存行情、账本、会议和记忆**。运行依赖和模型服务均与本机 WorkBuddy 分开。
它只维护虚拟账户，不接券商。模型接口为通用 **Chat Completions 兼容接口**，地址、模型和密钥在部署时填写。

新增默认多角色研究会议，详见 [会议室说明](MEETING.md)。你已有服务器时，推荐 [服务器部署方案](SERVER.md)：服务器主运行，GitHub保存代码/测试/手动补跑。下面保留完整Actions部署路线；两者选一个主调度端。

结果展示使用 [GitHub Pages静态复盘台](PAGES.md)：GitHub定时读取R2的日报和会议结果，生成网页并发布。配置 `DAILY_EXECUTOR=server` 后，网页发布仍会继续。

## 1. 改造包含什么

- 工作流：`.github/workflows/daily.yml`，北京时间 **20:37、21:37**，周一至周五启动，再由 XSHG 交易日历判断休市。
- 20:37 做首次处理；21:37 是失败补偿。完成的交易日不重复成交、不重复调用成功的 AI 复盘。
- 行情：腾讯不复权日线和收盘快照；只有缺历史的股票才补日线，日常直接追加当日快照。
- 选股：收盘涨停、剔除 ST/退市、至少120条历史日线、120日价格区间位置 <10%。
- 执行：事前冻结候选 → 次日按开盘条件模拟买入 → 下一交易日收盘平仓；涨停顺延一次；跌停无法卖出则继续持仓并估值。
- 状态：云端独立账本承接原快照 **100,013.62元空仓现金**；原账本和历史计划不修改、不事后补交易。
- 报告：中文 Markdown、可独立打开的 HTML、可复核 JSON；在 Actions 的 Artifacts 下载。
- 复盘：四席独立分析 → 一席交叉审计 → 主持人汇总；逐席保存进度，合并共享/角色记忆与待验证提案；通常六次成功模型调用。
- 程序计算金额，模型解释结果；模型没有改规则、改账本、下单或执行工具的权限。交易参数仍在 `config.json`，会议设置独立位于 `meeting.json`。

## 2. 与原版的明确差异

新版本名为 `v2.1-cloud1`，不能把它和原版收益序列无差别拼接：

1. 原版 gate C 是人工判断外围市场。云端默认记为 **未验证、不通过**，A/B 两项通过时用 **3500元/只**档；不会静默默认“无风险”。
2. 原代码把分时第一条（常为09:30）的成交额用作竞价额。云端不会把第一分钟成交额当成纯竞价额。支持 Tushare `stk_auction_o` 精确数据；缺少该股数据时标记无法判定，不模拟买入。
3. `stk_auction_o` 官方说明为每个交易日 **20点更新**，因此完整结算安排在20:37。日线/开盘/收盘也须有交易日期与完整性证据。
4. 同一股票已有持仓时不重复买入；买入只使用开盘前已有现金，不使用当天收盘卖出所得资金。
5. 收盘触及跌停时保守判为无法卖出；之后开盘高于跌停价才模拟卖出。这个模型不还原逐笔排队。
6. 云端观察期从零计数，首10笔完成交易平均收益为负则停止新增买入，继续管理持仓和出报告；不会由 AI 自动解除。
7. 股票池来自原包，不自动引入新上市股票。名称/ST标记在每日行情中更新；长期使用需定期审核、扩充股票池。
8. 模型不自动调参；复盘建议要另做研究验证。HTML中的模型内容按纯文本转义显示。

## 3. 需要配置的密钥和变量

GitHub 仓库 → **Settings → Secrets and variables → Actions**。

### Secrets（机密）

| 名称 | 是否需要 | 内容 |
|---|---|---|
| `R2_ACCESS_KEY_ID` | 必须 | R2 S3 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | 必须 | 与上面配套的 Secret Access Key |
| `AI_API_KEY` | AI复盘必须 | 你的兼容模型服务 API Key |
| `TUSHARE_TOKEN` | 完整R0竞价成交判定需要 | 有 `stk_auction_o` 权限的 Tushare Token；普通Token不代表已有该接口权限 |

不配置 Tushare 时，选股和 AI 报告仍能运行，但缺竞价额的候选不会成交。已配置却无权限/返回错误时，任务失败，避免错误结算。
没有额外购买或开通任何服务。该数据权限由用户自行选择和配置。

### Variables（普通配置）

| 名称 | 示例/含义 |
|---|---|
| `R2_ENDPOINT_URL` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`；使用R2桶所属地区对应S3端点 |
| `R2_BUCKET` | 已创建的私有桶名，例如 `ashare-paper-state` |
| `R2_PREFIX` | `wuming-v2`，可省略；同一账户的所有任务必须一致 |
| `AI_BASE_URL` | 服务商文档中的兼容接口基础地址，通常以 `/v1` 结尾；程序追加 `/chat/completions` |
| `AI_MODEL` | 服务商提供的模型 ID，不能填聊天产品里的显示名或账号名 |
| `DAILY_EXECUTOR` | 仅GitHub仓库变量；`server` 跳过Actions定时运行，保留手动补跑；省略/`github` 沿用Actions主运行 |

R2 Token 应给指定桶 **Object Read & Write** 权限。Bucket 保持私有，不需要公开域名，也不需要 Cloudflare 全局 API Key。
GitHub 的日常工作流只需仓库读取权限，不需要另配 GitHub PAT。
AI服务需要支持 `messages`、`max_tokens` 请求及 `choices[0].message.content` 文本响应，模型需能按提示输出JSON。使用HTTPS地址；不支持只有专有接口格式的服务。所有角色默认共用同一密钥，无需额外会议密钥。

## 4. 首次在 Mac 准备和上传 R2

在“无名游资A股AI交易系统V2版本”根目录操作。建议使用 Python 3.14，与测试和云端一致。

```bash
cd "$HOME/Documents/无名游资A股AI交易系统V2版本"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r cloud/requirements.lock
export PYTHONPATH="$PWD/cloud"
```

只读取原数据库，提取最近200个市场交易日到独立云端目录（通常远小于原1.1GB数据库）：

```bash
python -m ashare_cloud.cli init \
  --legacy-root "$PWD/大投机家利弗莫尔" \
  --state-dir "$PWD/.cloud-state-seed"
python -m ashare_cloud.cli status --state-dir "$PWD/.cloud-state-seed"
```

初始化会核对原10/19结算为完全空仓、现金与总资产相等。已有本地状态不会被覆盖。

在 Cloudflare 创建 R2 桶和对应的 S3 Token。然后：

```bash
cp cloud/.env.example .cloud.env
chmod 600 .cloud.env
open -e .cloud.env
```

在编辑器填写配置；特殊字符或包含空格的值用单引号包围。不要把真实密钥发到聊天或提交仓库。

```bash
set -a
source .cloud.env
set +a
python -m ashare_cloud.cli bootstrap --state-dir "$PWD/.cloud-state-seed"
```

`bootstrap` 只允许创建不存在的 `R2_PREFIX/HEAD.json`，不会重置已有远端账户。
不需要首次上传原始全包，也不要将原1.1GB数据库提交到GitHub。

## 5. 上传 GitHub 并启用每日任务

建议创建**私有仓库**。为避免夹带原始数据库和工作记忆，优先导出仅含代码的目录：

```bash
python cloud/export_repository.py "$HOME/Documents/wuming-cloud-repository"
```

将导出目录的**内容**放到仓库根目录，保证 GitHub 能看到 `.github/workflows/daily.yml`。
通过 GitHub Desktop 或常用 Git 工具提交上传；不要把 `.github` 藏在又一层文件夹里。

随后：

1. 填写上一节的 Secrets 和 Variables。
2. 确保工作流在默认分支上，且仓库 Actions 已启用。
3. 在交易日20:00后，打开 **Actions → A股每日模拟盘 → Run workflow**，日期留空。
4. 成功后在该次运行的 **Artifacts** 下载 `daily-report-...`，打开日期对应 HTML。
5. 检查执行摘要：数据覆盖率、AI状态、竞价数据是否可用、R2最后结算日期。

**第一次运行不会倒推今天该买什么**。它保存空仓结算并冻结下一交易日候选；从下一个交易日开始依据事前计划模拟成交。
周末手动运行会显示“休市日，跳过”，不产生日报。

## 6. 数据保存与失败恢复

R2 的结构：

```text
wuming-v2/
  HEAD.json                         当前有效状态指针
  databases/<SHA256>.sqlite.gz       按内容寻址的数据库压缩包
  states/<唯一编号>.tar.gz           账本、计划、行情快照、报告、会议、记忆、提案
```

每次先上传新文件，最后以 `If-Match` 原子更新指针。并行任务发生冲突时，较旧状态不能覆盖新状态。
GitHub工作流也串行执行；不取消正在保存账本的任务。R2状态是权威账本，Actions缓存和Artifacts都不作为唯一账本。
AI失败时财务状态已经提交：工作流会标红，21:37或手动重跑从未完成的会议环节继续。已保存成功的AI响应复用；API响应后、保存前机器崩溃可能导致该环节重调。
报告HTML/JSON包含当前持仓和选股，下载附件按私有研究资料处理。

### 历史行情补齐与断点恢复

首次补齐数千只股票时，默认使用2个线程，两个腾讯入口和全部重试共用至少0.35秒的请求间隔。
HTTP 403/429、超时等错误触发全局冷却和渐进降速；单只最多4次尝试，并轮换入口。
服务端给出 `Retry-After` 时遵守等待时间；若等待超出本次时间预算，则保存进度退出。

每处理500只，或距离上次保存超过3分钟，会把已提交的行情数据库、未完成缺口和诊断同步到R2。
连续8只取数失败，或用完40分钟抓取预算时，停止新增请求并保存剩余进度，给上传与复盘留出运行时间。
突然断电/被强制取消只能恢复到最近一个成功检查点；R2保存失败或版本冲突会立即中止，不能假装已保存。
这些行情检查点不会修改资金、持仓和冻结计划；只有完整性检查通过，才进入模拟结算。

任务失败后，在 **Actions → A股每日模拟盘 → Run workflow** 选择 `main`、`mode=run`，当天日期留空。
新运行从R2恢复，跳过已经补齐的股票，只请求剩余缺口；今天的收盘报价不会掩盖更早的历史缺口。
更新代码后应点击 **Run workflow**，不要用旧运行的 **Re-run jobs**，后者仍使用旧提交。
跨交易日会保留未补齐的行情缺口，但这不解除账本的“不可跳过未结算日、不可事后建计划”限制。

Actions附件新增 `diagnostics/history-YYYY-MM-DD.json`，包含成功/失败/未处理数量、上一交易日覆盖率、
HTTP状态或超时类别、失败股票代码与停止原因；不记录服务端原文、请求凭证或模型配置。
R2中的 `history-pending.json` 记录每只股票尚未补齐区间的原始截止日期，不公开到Pages。
97%的覆盖率要求和最多3%的历史请求失败比例保持不变；不会通过降低完整性要求来强行选股。
没有 `TUSHARE_TOKEN` 时可以补行情、选股与复盘，缺少可靠竞价额的候选仍不模拟买入。

重新下载状态到一个**不存在或为空**的目录：

```bash
python -m ashare_cloud.cli restore --state-dir .cloud-state-restored
python -m ashare_cloud.cli status --state-dir .cloud-state-restored
```

若漏掉了一个交易日，系统不会越过它悄悄记账。已有该日完整归档时，可在 Actions 的 `trade_date` 输入该日补跑；
未归档的历史成交不能用今天的实时接口伪造。需补齐可验证历史数据和缺失计划的处理记录后恢复。
历史补跑也不会事后创建已经开盘的交易日计划，因此严重中断应人工检查后恢复，而不是强制覆盖。

不要给 `HEAD.json` 或它引用的状态/数据库配置自动过期。历史不可达版本可后续做引用检查后清理，当前实现不自动删除。
R2桶存储量会随不同日数据库版本增加；当前不含自动压缩历史版本的保留策略。

## 7. 本地检查

```bash
python -m pip install pytest pyyaml
PYTHONPATH=cloud python -m pytest cloud/tests -q
```

测试包括：T+1、空候选、重复执行、现金不足、股数取整、R0/R2/R4、计划被改、
行情日期错误、漏交易日、涨停顺延一次、跌停滞留估值、R2并发冲突、AI失败后重试、HTML转义。
测试里的模型响应及R2服务是模拟对象，不代表已经验证真实模型账户或R2权限。

要在本地运行实际当日流程，可在恢复的独立目录上执行：

```bash
python -m ashare_cloud.cli run --local --state-dir .cloud-state-restored
```

`--local` 不上传R2，不要把此结果和云端并行运行的结果混用。正常运行由选定的服务器或GitHub Actions负责。
`--skip-ai` 是显式检查模式，报告会注明没有AI复盘，不会伪装成功的模型响应。

`review --date YYYY-MM-DD` 只对已有结算日生成或恢复会议，不重新采集行情、成交或建计划。R2模式仍使用空的恢复目录；服务器入口会为每次尝试建立新目录。

## 8. 目前的边界

- GitHub定时触发可能延迟或漏触发，不承担集合竞价期间秒级采样。
- 海外运行节点访问免费行情接口的可用性需要首次云端运行确认；代码有重试、日期/覆盖率检查。
- 原始包数据不是实时数据；首次补齐数千只股票通常需要数十分钟。工作流超时55分钟，行情抓取预算40分钟，未完成时从R2检查点续跑；免费接口持续不可用时仍会明确失败。
- XSHG日历目前只覆盖到已公布年份；超出覆盖范围会报错，届时更新依赖并测试，不按普通工作日猜测。
- 样本中的手续费配置沿用原包，均为模拟费用假设；实际市场费率和成交规则需另行核实。
- 已完成代码和本地验证，不代表远端已部署；必须完成R2初始化、仓库上传和密钥配置。

参考：[GitHub定时任务](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、
[R2 S3接口与原子条件写入](https://developers.cloudflare.com/r2/api/s3/api/)、
[R2 Token权限](https://developers.cloudflare.com/r2/api/tokens/)、
[Tushare开盘集合竞价接口](https://tushare.pro/document/2?doc_id=353)。
