---
name: mobileworld-settings-brightness-endpoints
description: "Settings: set the brightness slider to its exact minimum or maximum endpoint."
version: 1.0.1
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

- In Display > Brightness level, drag the thumb to the rightmost on-screen
  point for maximum, or the leftmost for minimum. This crosses the track's
  boundary; leave `surface_index` unset. The slider clamps at its endpoint.
- The displayed percentage rounds: `100%` can appear before the exact maximum.

## Verification

- No additional adjustment or settings reopening is needed after this endpoint
  drag.
