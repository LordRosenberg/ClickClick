# Task demos

[README](../README.md) · [中文首页](../README.zh-CN.md) · [Task examples](task-examples.md)

These videos show complete successful Android tasks at **3× playback speed**. The original screen sequence is preserved. Cropping removes unused black recording margins; an adjacent panel explains the task. Animated README previews sample eight moments and link to the full videos.

## Notes to recipes / 笔记转食谱

[Watch the video](assets/demos/notes-to-recipes.mp4)

> Add the recipes from recipes.txt in Markor to the Broccoli recipe app.

Read three source recipes, carry their fields across apps, fill separate recipe forms and inspect the saved details. 本例展示长文本读取、跨应用记忆、多字段填写和保存后核验。

Recording: 10:02 → 3:21. Task execution: 596.3 seconds; 40 submitted actions.

## Image to expenses / 图片转费用记录

[Watch the video](assets/demos/image-to-expenses.mp4)

> Add the expenses from expenses.jpg in Simple Gallery Pro to pro expense.

Read three expenses from an image, then enter their names, amounts, categories and notes in the expense app. 本例展示图片信息提取、跨应用转录和多条记录处理。

Recording: 7:36 → 2:32. Task execution: 449.6 seconds; 34 submitted actions.

## Recurring calendar event / 重复日历事件

[Watch the video](assets/demos/recurring-calendar-event.mp4)

> In Simple Calendar Pro, create a recurring calendar event titled 'Catch up on Campaign' starting on 2023-10-17 at 0h. The event recurs daily, forever, and lasts for 45 minutes each occurrence. The event description should be 'We will organize business objectives. Remember to confirm attendance.'.

Configure the date, time, duration, description and recurrence, then inspect the saved event. 本例展示多个关联字段和重复规则的配置与核验。

Recording: 7:01 → 2:20. Task execution: 413.2 seconds; 22 submitted actions.

## Deduplicate expenses / 费用去重

[Watch the video](assets/demos/deduplicate-expenses.mp4)

> Delete all but one of any expenses in pro expense that are exact duplicates, ensuring at least one instance of each unique expense remains.

Inspect the expense list, compare candidate duplicates and remove the extra copy while preserving distinct records. 本例展示长列表检查、相似记录辨别与去重后的保留性核验。

Recording: 4:50 → 1:37. Task execution: 284.9 seconds; 15 submitted actions.

## Ordered playlist / 按顺序创建歌单

[Watch the video](assets/demos/ordered-playlist.mp4)

> Create a playlist in Retro Music titled "Blues Break 134" with the following songs, in order: Voices in the Hall, Silent Dreams

Create the playlist, locate both songs and verify the final order. 本例展示多页面导航、指定条目查找和顺序约束下的结果检查。

Recording: 3:55 → 1:18. Task execution: 228.0 seconds; 18 submitted actions.

## Weekly activity summary / 每周运动时长统计

[Watch the video](assets/demos/weekly-activity-summary.mp4)

> What was the total duration of swimming activities in the OpenTracks app this week? Assume the week starts from Monday. Express your answer in minutes as a single integer.

Identify the weekly interval, inspect swimming activities and report the total duration. The evaluated answer was **510 minutes**. 本例展示相对日期理解、类别与日期筛选，以及时长汇总。视频展示设备操作，答案来自该次执行记录并显示在说明面板上。

Recording: 3:32 → 1:11. Task execution: 206.5 seconds; 9 submitted actions.

## Recording provenance

All six recordings come from the September 18 `androidworld-sol-final-v1-20260918` batch. Each has an official score of 1, a successful agent result and completion within its original budget. They illustrate capabilities and are separate from the September 21 [115/116 benchmark result](androidworld-results-20260921.md).

The recordings include capture margins around worker execution, so recording duration differs from task execution time. The [media manifest](assets/demos/manifest.json) records task IDs, source/output SHA-256 digests, cropping, speed and duration.

To regenerate from the original episode recordings, install the project's `[decode]` dependencies and run:

```bash
python scripts/render_demo_videos.py \
  --episodes /path/to/batch/episodes \
  --output docs/assets/demos --speed 3
```

The renderer uses PyAV and Pillow, produces H.264 MP4 files and silent GIF previews, and checks each episode's successful result before rendering. Source recordings are retained separately from the published media.
