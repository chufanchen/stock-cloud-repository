# GitHub Pages 静态复盘台

架构：**服务器或GitHub Actions运行模拟盘和AI会议 → 保存到私有R2 → GitHub Actions只读构建网页 → GitHub Pages展示。** 网页发布不采集行情、不调用模型、不修改账本。完整部署步骤见 [项目README](../README.md)。

## 展示内容

- 每日总览：最新日报状态、模拟总资产、当日损益、现金、持仓市值、资产走势与候选。
- 历史日报：按日期查找，查看持仓、模拟成交、未买入原因与AI复盘。
- AI会议室：各席观点、问题、交叉审计、共识与分歧。
- 研究提案：假设、验证方案、否定条件与数据需求；统一显示待验证。

所有页面是HTML/CSS/少量JS，无需在服务器部署网页后端。浏览器不直接连接R2或模型服务；没有外部图表CDN依赖。路径兼容 `https://用户名.github.io/仓库名/`，手机布局自适应。

没有日报时显示等待页，不使用演示收益填充。AI失败时保留程序计算的账本数字，并标注待重试。若R2已有新结算但日报尚未归档，页面说明仍展示上一份日报。

## 发布设置

目标仓库：`chufanchen/stock-cloud-repository`，默认分支 `main`。

1. 仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
2. 在 **Settings → Secrets and variables → Actions** 填写R2配置。既有Secrets可直接复用；普通配置放Variables或Secrets都支持。
3. 根据每日计算端设置 `DAILY_EXECUTOR`：Actions用 `github`，服务器用 `server`。此变量不影响网页发布；使用服务器时需另行实际部署并启动timer。
4. 手动运行 **Actions → 发布复盘网页 → Run workflow**。构建和部署完成后，环境 `github-pages` 会显示网站链接。

当前仓库已采用公开代码与公开Pages部署，R2保持私有。如果迁移到私有仓库，需确认账户套餐支持Pages；私有代码仓库也不自动意味着网站私有。参见 [官方Pages工作流说明](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) 和 [访问控制说明](https://docs.github.com/en/enterprise-cloud@latest/pages/getting-started-with-github-pages/changing-the-visibility-of-your-github-pages-site)。

## 凭据

网页流程只使用：

| 字段 | 用途 |
|---|---|
| `R2_ENDPOINT_URL`、`R2_BUCKET`、`R2_PREFIX` | 读取指定项目的归档，支持Variables或Secrets |
| `R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY` | 复用已配置的R2凭据 |
| `R2_PAGES_ACCESS_KEY_ID`、`R2_PAGES_SECRET_ACCESS_KEY` | 可选：为网页提供单独的对象只读凭据；两个字段须同时填写，填写后优先使用 |

不需要AI、Tushare或额外GitHub PAT。部署使用GitHub自动提供的令牌，构建任务只有仓库读取权限；部署任务单独获得Pages部署权限。需要更小权限时，在Cloudflare创建指定桶的Object Read凭据并填入两个 `R2_PAGES_*` Secrets。

只下载R2的小状态压缩包，不下载行情数据库。构建器提取需要的日报、会议和提案，再逐字段生成网页；原始状态包、账户文件、模型提示词、各席记忆、策略配置、凭据和行情数据库不会放进站点。

网页公开展示的是模拟账户金额、候选/持仓/成交及模型发言。模型发言按文本转义，HTML、图片和链接不会被当作可执行内容。发布流程没有自动识别或脱敏模型文字中所有个人信息的功能，因此不要向复盘上下文添加不适合公开的信息。

## 更新时机

- 周一至周五北京时间 **21:17、22:17、23:17** 各检查一次最新R2归档；网页工作流本身不按交易日历跳过节假日。
- 手动运行发布工作流可立即更新。
- 更新网页代码后自动构建；若用Actions运行原交易工作流，其结束后也会触发网页更新。
- GitHub定时任务可能延迟，以上不是精确发布时间承诺。它读取当前有效R2快照，遇到网络或数据错误时让构建失败，旧网站保持原状。

交易日的首轮服务任务在20:37开始，21:37为重试。网页多次发布仅重复读取结果，不重复交易或调用模型。

## 本地预览

从R2生成正式页面（环境变量已加载）：

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.pages --output _site
python3 -m http.server 8000 --directory _site
```

浏览器打开 `http://localhost:8000/`。也可用 `--state-dir <已恢复的本地状态目录>` 离线构建。输出目录必须不存在或为空，且与私有状态目录分开，防止错误发布旧文件；需要重建时使用一个新的输出目录。

`publication.json` 记录生成时间、最新日报日期和数量，可用来核对刷新情况。没有财务数据的首次站点会正常上线为空状态；第一个交易日有日报后无需改网页代码。
