---
name: mobileworld-documentsui-date-ordering
description: Files date display and chronological ordering conventions on MobileWorld API 34.
version: 1.0.0
app_aliases: [Files, DocumentsUI]
app: com.google.android.documentsui
interface_scope: system
device_profiles: [mobileworld_api34]
kind: app_core
role_sections: true
capability: interpret_file_dates
source: authored
---

# Files dates

## Shared

### Hints

- This Files UI exposes `Modified`, not a separate creation-time field. For
  ordinary oldest/newest file ordering, including requests phrased as "creation
  date", use the displayed file date; do not hunt for an unavailable field.
  This does not establish that creation and modification times are equal. If
  the request explicitly distinguishes those timestamps, do not substitute one
  for the other.
