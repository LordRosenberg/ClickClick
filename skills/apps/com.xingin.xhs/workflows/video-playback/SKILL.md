---
name: xiaohongshu-video-playback
description: 打开小红书视频并用有序多帧证据判断是否真实播放。
version: 1.0.0
app: com.xingin.xhs
kind: workflow
capability: verify_video_playback
tags: [video, playback, temporal]
source: authored
---

# Verify video playback

## Procedure

1. 打开目标视频并等待详情或视频流表面可观察。
2. 调用时间序列观察，比较按顺序采集的画面内容是否推进。
3. 如果多帧没有建立播放状态且画面提供明确的播放控件，只点击一次该控件，再重新采集时间序列证据。

## Verification

- 至少两张有序画面显示视频内容、进度或其他可靠动态信息随时间推进。
- 若时间序列不完整或无变化，播放状态仍未证实。

## Hints

- 视频流中上滑通常会切换到下一个视频；只在用户目标需要切换时使用。

## Anti-patterns

- 不要根据「暂停」、Pause、播放图标或单张截图断言视频正在播放。
- 不要为了试探控件文字而反复点击播放区域。
