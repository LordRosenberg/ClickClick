---
name: google-maps-arrival-estimates
description: Interpret Google Maps driving-duration ranges and arrival estimates.
version: 1.0.2
app_aliases: [Maps, Google Maps]
app: com.google.android.apps.maps
interface_scope: app
kind: app_core
role_sections: true
source: authored
---

# Google Maps arrival estimates

## Shared

### Hints

- Maps defaults to `Leave now`. For a requested departure time, select that
  time in the route options before using its traffic estimate; a duration
  obtained under `Leave now` describes a different departure condition.
- `Typically A to B` is a duration range. If `Arrive around T` equals the
  selected departure time plus B, T is the upper bound, not a separate typical
  arrival estimate. For an approximate ETA, retain both arrival bounds and
  their traffic uncertainty when reporting or messaging the result instead
  of presenting that upper-bound label as the sole expected arrival.
