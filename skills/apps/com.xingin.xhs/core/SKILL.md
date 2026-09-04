---
name: xiaohongshu-core
description: 小红书稳定的页面结构、导航与遮罩处理常识。
version: 1.0.0
app: com.xingin.xhs
kind: app_core
capability: app_navigation
tags: [xiaohongshu, navigation]
triggers: [小红书, 红书, XHS, xiaohongshu]
source: authored
---

# Xiaohongshu core

## Hints

- 内容通常以图文、视频或直播卡片展示；先用当前页面标题、Tab 和卡片上下文判断所在表面。
- 首次启动、登录、更新或权限遮罩可能挡住应用内容；只处理继续当前目标所必需的遮罩。
- 图文帖与视频帖的点赞、收藏和评论入口通常位于详情页下方；无障碍索引不足时先观察当前截图。

## Anti-patterns

- 不要把某个功能在旧版本中的固定坐标当作稳定入口。
- 不要因为一个 Tab 显示已选中就假设再次点击它不会打开附加菜单。
