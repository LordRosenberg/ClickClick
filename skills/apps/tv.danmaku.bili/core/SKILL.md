---
name: bilibili-core
description: B站稳定的主要表面、搜索入口与常见遮罩处理常识。
version: 1.0.3
app: tv.danmaku.bili
kind: app_core
capability: app_navigation
tags: [bilibili, navigation]
triggers: [B站, 哔哩哔哩, bilibili]
source: authored
---

# Bilibili core

## Hints

- 顶部搜索入口可能是搜索框或放大镜图标，无障碍名称不一定包含「搜索」。
- 青少年模式、登录、更新或权限遮罩可能挡住内容；只处理继续目标所必需的遮罩。
- 视频详情通常包含播放器、标题、UP 主信息和相关推荐，可用这些上下文区分搜索结果与已打开的视频。
- 搜索结果顶部可能先出现合集/专题模块，其中横向内嵌的卡片仍是可见视频；当任务按“第一个可见视频”或其他顺序选择且未排除合集时，应把这些内嵌视频按页面阅读顺序纳入候选，不要默认从后面的普通大卡片开始。

## Constraints

- 播放器的「暂停」名称或双竖线图标表示可执行暂停，不证明当前已暂停；「关闭弹幕/开启弹幕」只控制弹幕，不负责显隐播放控件或切换播放状态。 | scope: tv.danmaku.bili 视频详情播放器中判断或切换播放状态时 | evidence: repeated GPT-5.4 real-device traces 2026-09-03

## Anti-patterns

- 不要假设底部「首页」一定是带文字的普通 Tab；它可能以突出图标呈现。
- 搜索入口索引不可靠时不要编造 index，应先观察当前画面。
- 不要用未刷新的控件标签替代播放状态证据。
