# GitHub Pages：只读展示结果

本地 runner 的 `--cloud` 完成或失败时导出公开字段结果包至 R2。GitHub 工作流只读 `publications/HEAD.json` 和其指向的一个 JSON 文件，不读取完整账户、数据库、模型输入或研究记忆。

## 配置

仓库 Settings → Pages → Source 选 GitHub Actions。设置 R2_ENDPOINT_URL、R2_PREFIX；R2_PUBLIC_BUCKET 可指定独立结果桶。R2_PAGES_ACCESS_KEY_ID / R2_PAGES_SECRET_ACCESS_KEY 推荐只读结果桶。未配置时兼容原R2凭据；独立桶才提供明确的备份访问隔离。

先在本地运行 `PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.desktop task --command publish`，再运行 Actions 的“发布复盘网页”。未找到结果包时构建失败，保留现有网站，不用空站覆盖。

网站包括总览、历史日报、会议、研究提案、早盘预案、回测结果。公网可见账户汇总、展示中的持仓与成交、模型发言及冻结候选。AI文本转义后展示。字段白名单不会自动识别已写进模型发言中的敏感自然语言，输入给模型前仍应保持资料范围。

早盘预案来自账本真实冻结计划；回测只显示已实际运行的归档重放，缺失时展示缺失，不构造收益。仅有研究会议而没有账户日报的独立测试仍不并入正式账户网站。

发布定时为北京时间09:17、21:17、22:17、23:17；修改网页代码或手动运行也触发。网站同时显示最近任务状态和日报日期，以免把旧报告当成新报告。GitHub调度可能延迟。手动旧模式 `pages --legacy-r2` 仅供迁移核对，工作流不使用它。

本地预览：

```bash
PYTHONPATH=cloud .venv/bin/python -m ashare_cloud.pages --state-dir .local-runtime/state --output /tmp/wuming-site
.venv/bin/python -m http.server 8768 --bind 127.0.0.1 --directory /tmp/wuming-site
```

输出目录必须为空且与私有状态分离；不要在项目根目录启动面向外网的文件服务器。
