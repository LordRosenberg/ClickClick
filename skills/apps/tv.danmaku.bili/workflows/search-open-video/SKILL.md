---
name: bilibili-search-open-video
description: 在B站搜索指定视频并从真实结果列表打开匹配内容。
version: 1.0.0
app: tv.danmaku.bili
kind: workflow
capability: search_open_video
tags: [search, video]
source: authored
---

# Search and open a video

## Procedure

1. 处理挡住内容的必要遮罩，进入可搜索表面。
2. 聚焦搜索框，输入用户给出的查询词并显式提交搜索。
3. 区分联想词页面与真实结果列表，从结果卡片或标题打开匹配内容。

## Verification

- 当前页面是视频详情或播放器表面，而不是搜索建议或结果列表。
- 标题或可见上下文与用户指定的视频相符。

## Hints

- 如果当前屏幕已经是目标视频，可直接依据详情上下文验证，无需重复搜索。

## Anti-patterns

- 输入查询后不要把联想候选当作已经完成的搜索结果。
