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
- `/tasks/:id` — 任务详情（Timeline 视图 + Trace 流 Tab）
- `/device` — 在线设备列表（serial / 型号 / 忙闲）+ scrcpy 提示
