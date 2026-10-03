---
name: mobileworld-documentsui-file-selection
description: "Files: date-sort and range-select files for compression or deletion, and preview unclear images."
version: 1.0.5
app: com.google.android.documentsui
interface_scope: system
device_profiles: [mobileworld_api34]
kind: workflow
capability: select_local_files
tags: [files, attachments, images, preview, selection, compress, delete]
source: authored
---

# Select files in DocumentsUI

## Procedure

### Hints

1. For a date cutoff, use the overflow menu > Sort by > date order to group
   qualifying files; locate the cutoff boundary before selecting the range.
   Keep range drags inside file rows, above the system navigation area.
2. To select a continuous range for compression or deletion, use `drag` with
   `hold_before_move: true` from an unselected first file icon to the last
   row's icon in one gesture; do not separately long-press the start row first.
   To extend an existing selection, start from the next unselected row.
   Every intervening row must meet the requested conditions.
   Use individual selection for noncontiguous files.
3. When an image thumbnail is insufficient to distinguish the requested
   visual feature, `Grid view` provides larger thumbnails. The separate
   `Preview the file …` control opens a viewer without submitting the file;
   if an app chooser appears, choose Gallery > Just once. Back returns to
   the picker. Select directly when the thumbnail is already clear.

## Verification

- Use the returned screen's selection count and highlights to check the
  intended group before applying an operation. Do not extend a range through
  unknown or excluded rows.
- Previewing is not attachment submission. Return to the picker to choose
  the intended file; do not preview every candidate by default.
