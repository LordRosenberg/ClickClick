---
name: permission_dialogs
description: >-
  系统/厂商权限与同意弹窗。出现允许、拒绝、仅在使用中允许、去设置等遮罩时使用。
version: 0.1.0
kind: generic
tags: [popup, permission, system]
triggers:
  - 权限弹窗
  - 允许通知
  - 允许位置
source: authored
---

# Permission dialogs

## Hints

- 系统或 OEM 权限对话框，常盖住应用主界面。
- 首次启动后的通知权限卡片。
- 带「去设置」的深层跳转按钮（可能离开当前 App）。

- 任务需要该权限时：点明确的允许类按钮（允许 / 始终允许 / 仅在使用中允许）。
- 任务不需要时：可点取消 / 拒绝，不要强行授予无关权限。
- 处理完遮罩后再继续用户目标步骤；不要额外发明产品流程。

## Anti-patterns

- 不要反复点弹窗背后的变暗区域；要点对话框上的明确按钮。
- 「去设置」会离开 App，仅在任务确实需要时再跟。

## Executor notes

- 树里看不到弹窗铬时，调用 `observe_screen(mode=current)`，勿猜 index。
- 弹窗已消失则忽略本 skill，以当前屏幕为准。
