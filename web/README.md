# ClickClick Console (web)

组件化观测平台：React + Vite + TypeScript + Tailwind CSS + shadcn/ui。

## 开发

```bash
cd web
npm install
npm run dev      # Vite dev server，http://127.0.0.1:5173
```

开发期 Vite dev server 会把 `/api` 代理到 Control API（默认 `http://127.0.0.1:8080`，
可用 `CLICKCLICK_API_PORT` 覆盖）。先启动后端（`clickclick-api`），再启动前端。

## 构建

```bash
npm install
npm run build    # 产物输出到 web/dist/
```

构建产物由 Control API 的 `StaticFiles` mount 托管（指向 `web/dist`）。
`web/dist` 不存在时后端优雅跳过前端挂载，API 仍可用。

## 结构

- `src/api/` — 类型定义、fetch 封装、SSE（EventSource）封装
- `src/state/` — React Query hooks（timeline + SSE merge）
- `src/views/` — 路由级视图（任务列表、任务详情、设备）
- `src/components/` — Timeline 轨道、Step Inspector（SoM 叠加 / 语义树 /
  Manager / Executor 面板 / fingerprint diff）、Trace 流、shadcn/ui 基础组件
- `src/components/ui/` — shadcn/ui 基础组件（button / card / tabs / badge /
  scroll-area / separator）

## 路由

- `/` — 任务列表 / 仪表盘（含提交表单与失败列表）
- `/tasks/:id` — 任务详情（Timeline、已保存的观测截图、Trace 流）
- `/device` — 在线设备列表（serial / 型号 / 忙闲）与截图查看说明
- `/setup` — 桌面连接配置：模型 API、设备连接、MCP 注册与版本更新；通过安装器或 `clickclick setup` 打开授权入口

## Skill 学习轨迹

原任务详情页的 **Skill 学习** tab 显示源任务绑定的 Learner 连续分析、Reviewer 独立审查/证据读取，以及累计请求、动作和时间快照。消息和原始输入按需展开；未知 token 成本不补成零。已生成的 pending 候选可查看审查/验证元数据，并打开现有技能差异页。

探索和普通复验记录通过关联任务打开原有详情页，使用原 Timeline、Trace、模型交流和回放组件。数据库来自现有 Console data root 或 `CLICKCLICK_CONSOLE_RUN_ROOTS` 配置的只读外部根；任务不存在或未发现时显示 unknown，不读取 artifact 中提供的任意数据库路径。旧会话仅展示当时持久化的内容，未保存的 Reviewer 轮次无法追补。API 个人优化会保留跳过/中止结果；外部实验结束结果是否可见取决于对应原生记录是否保存。

列表响应按会话和结束结果分页（默认/最多每页20项），每会话默认最近60条轨迹、最多100条；页面明确显示总数，并可向前翻页查看旧记录。分页仅影响展示，原始 artifact 不删除，完整 catalog ref 保留；provider usage 明细只按需读取原记录，列表仅提供完整已知的 token 聚合。
