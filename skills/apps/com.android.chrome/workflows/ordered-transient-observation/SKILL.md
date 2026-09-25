---
name: chrome-ordered-transient-observation
description: 在网页中通过重复交互逐项读取短暂显示的信息，保留相同值的独立出现次数与顺序。
version: 0.1.1
app: com.android.chrome
interface_scope: app
kind: workflow
capability: ordered_transient_observation
tags: [browser, sequence, repeated, transient]
source: authored
---

# Record ordered transient observations

## Procedure

For each required occurrence, use a repeated observe-record-act cycle: read the current displayed item, append that occurrence and its observation source to one note, then perform exactly one advancing interaction. On the next decision, repeat from the new current observation.

Treat an item already displayed beside the advancing control as the first occurrence unless the page or instruction explicitly says it is only a sample. Do not assume every required item appears only after an action: the final required action may replace the last item with the answer form. Determine the page's actual observe/advance order from the current UI and preserve it in the plan.

Equal adjacent displayed values are separate occurrences unless independent evidence proves otherwise. Preserve their positions in order. A successful dispatch followed by `visible_change=none` says only that the compared rendering is equal. A confirmed interaction acknowledgement is mechanical evidence of target handling, not proof of the displayed value's meaning. When acknowledgement is unavailable and the occurrence affects later counting or calculation, note the exact facts and keep the interpretation unresolved instead of deleting the occurrence or blindly retrying.

## Verification

Before calculating or submitting, read the note and verify that every required ordinal is present in order, including duplicates, with no occurrence inferred solely from a distinct-value count.
