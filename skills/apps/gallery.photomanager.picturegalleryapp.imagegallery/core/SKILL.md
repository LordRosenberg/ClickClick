---
name: gallery-photo-selection-core
description: Select photos by content and group metadata, and share saved edits.
version: 1.0.1
app: gallery.photomanager.picturegalleryapp.imagegallery
interface_scope: app
kind: app_core
role_sections: true
source: authored
---

# Gallery photo selection

## Shared

### Hints

- When selecting photos by several conditions, establish each requested
  condition before including a photo. Matching image content alone does not
  establish its date or other metadata. Reuse clear evidence already obtained;
  this does not require a separate inventory or repeated per-photo checks.

## Execution

### Hints

- `Crop/Rotate` saves a separate image; the viewer may still show the original.
  Use the saved filename/location to select the new copy from the grid or media
  picker before sharing. An attachment selected before cropping is still the
  original and must be replaced with the saved copy.
- The Photos grid groups thumbnails under date headers. A group's header can
  scroll above the viewport while its photos remain visible. Preserve a known
  group association when correspondence is clear; otherwise reveal that header.
  Do not infer a missing date from the next visible header or the task's range.
- Use visible thumbnails and established group metadata to select matching
  photos together. If a required condition is still unknown, check only that
  condition for the affected photo or group; open details only when the grid
  cannot resolve it. Do not collect unrelated fields or recheck satisfied ones.
