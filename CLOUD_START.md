# 部署入口

完整指南见 [README.md](README.md)，包含代码结构、环境变量、R2初始化、GitHub Actions与服务器部署、Pages发布，以及补跑和故障排查。

选择一个每日计算端：

| 方案 | 仓库变量 `DAILY_EXECUTOR` | 网页发布 |
|---|---|---|
| GitHub Actions执行每日流程 | `github`，或不设置 | GitHub Actions → GitHub Pages |
| 服务器执行每日流程 | `server`，并实际启用服务器timer | GitHub Actions → GitHub Pages |

两者共用私有R2状态和通用模型接口。已有R2账户无需再次执行 `init/bootstrap`；`server` 变量本身不会安装或启动服务器任务。

专题说明：[服务器](cloud/SERVER.md) · [Pages](cloud/PAGES.md) · [AI会议](cloud/MEETING.md) · [TickFlow](cloud/TICKFLOW.md)。
