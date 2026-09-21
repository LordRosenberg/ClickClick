---
name: androidworld-settings-brightness-endpoints
description: Set AndroidWorld API 33 display brightness to an exact minimum or maximum value using the Settings brightness slider.
version: 1.0.0
app: com.android.settings
device_profiles: [androidworld_api33]
kind: workflow
capability: set_brightness_endpoint
tags: [settings, display, brightness, slider]
source: authored
---

# Set an exact brightness endpoint

## Procedure

For an exact maximum or minimum, drag the current brightness thumb toward the
corresponding screen edge far enough to pass the visible end of the slider
track; the control will clamp the value to its endpoint. If the displayed value
is still 99% when setting the maximum, continue dragging right from the current
thumb. Do not tap the slider's center to focus it, because that changes the
brightness to an intermediate value.

## Verification

Verify the displayed percentage after the drag. A slider that merely looks
almost full is not evidence of the exact maximum.
