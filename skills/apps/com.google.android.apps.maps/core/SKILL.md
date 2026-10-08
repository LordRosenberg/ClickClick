---
name: google-maps-arrival-estimates
description: Interpret Google Maps driving-duration ranges and arrival estimates.
version: 1.0.4
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
  arrival estimate; do not present that upper bound as the typical arrival.
- Follow any explicit output format required by the task. Otherwise, when Maps
  gives a duration range and the task asks for arrival time, add both bounds to
  the selected departure time and report the estimated arrival interval. When
  Maps gives a single value, report a single value. Label derived arrival times
  as estimates; do not claim that Maps displayed them.
