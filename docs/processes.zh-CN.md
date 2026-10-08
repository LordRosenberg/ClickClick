# 进程拓扑

[English](processes.md) · [中文首页](../README.zh-CN.md) · [架构](architecture.zh-CN.md) · [部署](deployment.zh-CN.md)

Control API 承载 Agent Harness，包括角色编排、任务会话、设备访问和上下文管理。默认使用 Planner + Executor，支持按需 Reviewer。会话数据使用本地 SQLite 和产物存储，无需单独部署数据库或会话服务。

| 入口 | 进程关系 |
| --- | --- |
| Console 与本地设备 | 浏览器 → 内嵌编排器和本地 Driver 的 Control API → Android 设备 |
| HTTP MCP | 助手 → 独立 HTTP MCP 进程 → Control API → 设备 |
| stdio MCP | 助手 → 轻量 stdio 适配器 → Control API → 设备 |
| 远程设备主机 | Control API → Driver RPC 主机 → 已连接设备 |
| 独立任务 | `clickclick-agent` → 配置的本地或远程 Driver → 设备 |
| Fixture 测试 | Control API → fixture driver；确定性假 Agent 测试还会替换模型调用 |

HTTP MCP 使用独立事件循环和端口，将任务提交、进度查询及控制请求转发给后台；模型和设备操作在后台执行。多个 MCP 客户端可共用后台，断开助手连接不会取消任务。

构建后的 Console 由 Control API 提供，只有前端开发需要独立 Vite 进程。scrcpy 视频源专用于任务观测；Console 读取已保存截图，不获取或重置视频源。

[部署指南](deployment.zh-CN.md)介绍启动和设备访问，[本地 MCP](local-mcp.zh-CN.md)介绍助手接入，[评测指南](evaluation.zh-CN.md)介绍基准运行进程。
