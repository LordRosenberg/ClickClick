---
name: miui-clock-timer-entry
description: 在小米系统时钟的计时页设置时分秒，处理滚轮数值选择以及设置后不启动的任务。
version: 0.2.0
app: com.android.deskclock
device_profiles: [xiaomi_15_cn_android15]
kind: workflow
capability: timer_entry
tags: [clock, timer, wheel]
source: authored
---

# Set a timer duration

## Procedure

1. 在时钟中选择“计时”；“秒表”显示的是累计时间，不是倒计时设置。
2. 滚轮中央深色值是选中值；SeekBar 的无障碍标签也给出当前值。上、下方浅色数字只是相邻候选，点击整个 SeekBar 的中心不会输入数字。
3. 一次调整一个字段。在该列内部，从中央选中行向相邻行方向慢滑约一格，显式设置 duration_ms 约 600，起点和终点均在滚轮内部。根据当前图片确定列中心和行间距，不复用旧坐标。该页面向上滑通常增大数值、向下滑通常减小；先用一步后的选中值校准方向与步长。
4. 根据实际变化缩短或调整下一次滑动；避免长距离快速滑动造成惯性跨越多格。目标值已经选中时停止调整该列，再处理下一列。

## Verification

- 中央选中值及无障碍标签的时、分、秒均与用户要求一致。
- 对于“不启动”任务，“开始倒计时”按钮应仍然存在，停留在设置页即可；不要为验证而点击开始。

## Anti-patterns

- 重复点击整个 SeekBar 中心不能把它改成某个相邻数字。
- 连续大幅快速滑动、仅按上一步意图推断方向，会在循环滚轮中反复越过目标。
