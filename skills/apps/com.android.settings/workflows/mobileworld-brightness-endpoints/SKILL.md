---
name: mobileworld-settings-brightness-endpoints
description: "Settings: set the brightness slider to its exact minimum or maximum endpoint."
version: 1.0.0
app: com.android.settings
device_profiles: [mobileworld_api34]
interface_scope: system
kind: workflow
capability: set_brightness_endpoint
tags: [settings, display, brightness, slider]
source: authored
---

# Brightness endpoints

## Procedure

### Hints

- In Display > Brightness level, drag the thumb past the visible track's end
  toward the corresponding screen edge. The slider clamps at its endpoint.
- The displayed percentage rounds: `100%` can appear before the exact maximum.
  For an exact endpoint, finish beyond the track, rather than just near its end.

## Verification

- Use the resulting thumb position and percentage together; after a drag past
  the endpoint, no additional adjustment or settings reopening is needed.
