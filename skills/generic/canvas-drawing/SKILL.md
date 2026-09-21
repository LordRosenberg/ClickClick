---
name: canvas-drawing
description: 在画布或绘图应用中设置画笔与颜色、绘制或修改内容，并按要求保存；依据当前画布范围和实际笔迹工作。
version: 0.2.0
kind: generic
tags: [canvas, drawing, editing]
source: authored
---

# Canvas drawing

## Prepare

Identify the visible drawing surface separately from reference images, toolbars and
palettes. Use current surface bounds where available, with room for brush width.
Keep the whole stroke inside the visible canvas. Re-ground after zoom, pan, scroll
or panels change the viewport. Discover controls from the current UI; indices and
positions do not encode tool or color identity.

## Draw

Set the requested brush, color and style before the mark that depends on them.
Use current indices for indexed controls; coordinates describe the drawing gesture.
For a drag confined to an indexed canvas, include its surface_index and keep the
whole stroke inside the current bounds with a brush-width margin.
For each change of style, inspect the resulting requested mark before continuing
work that depends on it, using the next normal observation; an extra tool call is
not required for a clear result. Selecting several colors in succession does not create
marks in those colors. Focus alone does not establish an active brush setting.
For plain color sampling, use `inspect_image_regions` with the needed metrics.
For matching a reference to a visible palette, include the whole relevant palette
once: indexed controls in targets (index), unindexed areas in regions (current
model-image bounds). Use compare with references="all_regions", candidates="all_targets"
and top_k=3 for an indexed palette. Without indices, put each area in regions once
and select disjoint reference/candidate arrays of region:N IDs (1-based input order).
The tool expands comparisons internally. Omit metrics for compact rankings or
include them for RGB/Lab measurements too. Only index:N identities identify indexed controls.
The nearest supplied candidate is not necessarily an exact match; do not omit plausible
palette entries. Check ties_truncated before treating a Top-K list as complete.
A mixed/background-dominated region does not establish a thin stroke's color.
Correct the target or tool selection when the mark disagrees with intent; do not
infer that the app cannot change settings from a mis-targeted click.
Use freehand or available shape tools as appropriate to the request. The requested
mark can provide verification; do not add an unnecessary test stroke or shape.

## Modify

Preserve unrelated existing marks. Use selection, undo, eraser or other visible
editing tools for the requested change. Clear the entire canvas only when the task
permits removal of its contents; do not make clearing a prerequisite to drawing.

## Save

Save, export or submit only as requested. Choose visible format/location controls
from the current UI and inspect the actual result. Do not invent a required success
message, an updated reference image or an extra export when the task asks only to draw.
