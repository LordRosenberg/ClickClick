---
name: mobileworld-documentsui-file-selection
description: "Files: browse actual folders, range-select for compress/delete, and preview unclear images."
version: 1.0.2
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

- For a specified filesystem path, open the storage root and traverse its folders.
  The sidebar's `Documents`, `Images` and `Recent` views are indexed categories;
  an empty category does not establish that the named folder or file is absent.

1. To select a continuous range for compression or deletion, use `drag` with
   `hold_before_move: true` from the first visible row's file icon to the last
   row's icon. Every intervening row must meet the requested conditions.
   Use individual selection for noncontiguous files.
2. When an image thumbnail is insufficient to distinguish the requested
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
