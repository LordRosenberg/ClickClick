---
name: osmand-routing-and-markers
description: OsmAnd offline place search, ordered route-to-track saving, and map marker creation.
version: 1.0.1
app_aliases: [OsmAnd, OsmAnd Maps]
role_sections: true
app: net.osmand
interface_scope: app
kind: app_core
capability: manage_routes_tracks_and_markers
source: authored
---

# OsmAnd routes, tracks, and markers

## Shared

### Place search

- OsmAnd offline search is limited by the current map center and search radius.
  After entering the correct place name, an absent locality result does not mean
  the name is wrong. Use `INCREASE SEARCH RADIUS` repeatedly while that control
  remains available, then inspect the refreshed results.
- Match both the result name and its type. A country result is only a search
  context and must not be used as a requested village, town, or other locality.
  A comma-qualified query can return only its country component. If the map is
  far from the requested region, open that country result to recenter the map,
  reopen search there, and search the locality name without the country suffix.
  Verify the exact locality and its type before saving anything; recentering on
  the country is not task completion. Changing the query can reset the search
  radius, so avoid repeatedly alternating qualified and unqualified queries
  around the same distant map center. If already near the region, expand the
  radius or simplify the query as needed to reveal the exact locality.

### Routes and saved tracks

- Build an ordered route by setting the first requested waypoint as `From`, the
  last as `To`, and inserting every remaining waypoint with `Add` in the listed
  order. If `Add` closes the route panel, reopen `Route` before trying again.
- Before saving, inspect the route panel and confirm the visible `From`,
  intermediate, and `To` values match the requested order. A calculated route or
  navigation preview is not yet a saved track.
- Open route details and use `Save as new track file` to persist the route as GPX.
  Finish the save dialog. A route visible on the map without this save operation
  does not satisfy a saved-track task.

### Markers

- For coordinate markers, search the complete latitude/longitude pair and select
  the exact coordinate result row to open its location details. `Show on map`
  alone may display or center the location but does not create a marker.
- Use the location context action labeled `Marker` and confirm the flag appears.
  Do not treat a search pin, map center, or route endpoint as a saved marker.

### Verification

- Prefer the app's save confirmation and the resulting route/marker state. Do not
  add a mandatory reopen solely for confidence when the required state is already
  supported and the remaining time budget is tight.

This skill supplies no place names, coordinates, waypoint order, or success
verdicts. Explicit user instructions take precedence.
