---
name: mobileworld-documentsui-date-ordering
description: Files date ordering and archive-member extraction on MobileWorld API 34.
version: 1.0.1
app_aliases: [Files, DocumentsUI]
app: com.google.android.documentsui
interface_scope: system
device_profiles: [mobileworld_api34]
kind: app_core
role_sections: true
capability: interpret_file_dates
source: authored
---

# Files dates and archives

## Shared

### Hints

- This Files UI exposes `Modified`, not a separate creation-time field. For
  ordinary oldest/newest file ordering, including requests phrased as "creation
  date", use the displayed file date; do not hunt for an unavailable field.
  This does not establish that creation and modification times are equal. If
  the request explicitly distinguishes those timestamps, do not substitute one
  for the other.
- If an archive member cannot be opened directly, open the ZIP in Files,
  long-press the needed member, then use the selection menu > Extract to…
  and confirm the destination. For all members, use Select all before extracting
  once. Return to the destination and open the extracted files; an archive
  listing or extraction toast alone does not establish their contents.
