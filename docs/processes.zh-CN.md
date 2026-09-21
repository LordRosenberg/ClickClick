# 进程拓扑

[English](processes.md) · [中文首页](../README.zh-CN.md) · [当前架构](architecture.zh-CN.md) · [部署指南](deployment.zh-CN.md)

Control API 内嵌所选 Agent Harness，包含角色编排、工具会话和上下文／记忆管理。默认 Planner + Executor，支持按需 Reviewer。以 ContractAuthor 开始的旧运行时已删除。[架构图](architecture.zh-CN.md)中的模型、Harness、Session 与设备工具是逻辑职责边界，Console 和评测位于外围。Session 当前使用本地 SQLite／产物存储，不是单独部署的会话服务。

| 模式 | 进程关系 |
| --- | --- |
| 本地设备 | 浏览器 → 内嵌编排器和本地 Driver 的 Control API → Android 设备。 |
| 远程设备主机 | 浏览器 → 带编排器的 Control API → Driver RPC 主机 → 已连接设备。 |
| Fixture | Control API 使用 fixture driver；确定性假 Agent 测试还会替换模型调用。 |
| 独立任务 | `clickclick-agent` 使用配置的本地或远程设备访问运行任务。 |

构建后的 Console 由 Control API 提供；只有前端开发需要独立 Vite 进程。仓库固定版本的 scrcpy server 按需启动，观测与 Live 共享视频流。

[部署指南](deployment.zh-CN.md)介绍安装、端口、远程 Hub、HTTPS 和启动检查；[架构](architecture.zh-CN.md)介绍角色转换、会话工具、观测及轨迹持久化；[评测](evaluation.zh-CN.md)介绍独立 AndroidWorld 进程及 fixture 要求。

早期强制三角色合同循环和必须独立启动 Driver 的说明已由这些指南取代。带日期的实验报告保留为历史证据。
