---
name: miui-clock-stopwatch
description: 在小米系统时钟的秒表页运行、暂停或确认秒表状态，区分按钮动作和当前计时状态。
version: 0.2.0
app: com.android.deskclock
device_profiles: [xiaomi_15_cn_android15]
interface_scope: system
kind: workflow
capability: stopwatch_control
tags: [clock, stopwatch]
source: authored
---

# Control the stopwatch

## Hints

- 仅用于已匹配的小米系统界面；按钮文案与图标仍以当前观测为准，不套用到模拟器或其他系统时钟。

## Procedure

1. 进入“秒表”页；“计时”是倒计时功能。
2. 按钮名称表示点击后执行的动作。“继续”或三角播放图标表示当前已暂停，点击会恢复计时；“暂停”或双竖线图标表示当前正在运行，点击会暂停。“重置”会清除累计值和圈次，暂停任务无需重置。
3. 根据当前按钮与用户目标选择是否操作。已经是目标状态时直接交给 Reviewer 验证；不要为证明一次暂停而先启动，也不要在已暂停时再次点“继续”。

## Verification

- 运行：显示暂停控制，必要时用一次连续观测看到累计时间增大。
- 暂停：显示开始或继续控制；如完成条件要求时间停止，使用一次连续观测确认累计时间不变。
- 单帧中的非零累计时间既可能是暂停也可能是运行，不能单独证明状态。
