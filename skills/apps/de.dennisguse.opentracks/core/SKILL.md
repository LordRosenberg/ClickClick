---
name: opentracks-activities
description: OpenTracks activity recording and statistics, including search-based counting, exact type/date verification and record metadata editing.
version: 1.2.0
app_aliases: [OpenTracks, Open Tracks, OpenTracks Sports Tracker]
role_sections: true
app: de.dennisguse.opentracks
interface_scope: app
kind: app_core
capability: manage_and_query_activities
source: authored
---

# OpenTracks activities

## Shared

### Hints

#### Query activities

- For type-based counting, start with main-list Search, not the date/type filter
  dialog. Submit the query: typing alone may leave the old list visible. Search
  matches substrings in **name OR description OR activity type**, so results are
  candidates, not an exact type filter. It does not constrain dates. Use the
  requested type's spelling; if spacing/localization prevents a match, search
  a distinctive shorter stem and verify candidates rather than declaring zero.
- For duration/distance totals whose values are visible, the same search-first
  route can reduce work. Count/sum only candidates verified against both the
  requested type and date range; establish the end of results or the older-date
  boundary before concluding. Do not open every record by default.

#### Verify exact types

- Titles and descriptions are user-defined and can name the wrong sport. The
  screenshot's leading activity icon is useful; the tree's generic `Track`
  label does not identify a type. A matching icon is insufficient for the
  shared-icon families below. Different types in a family remain separate;
  do not merge them as synonyms unless the user explicitly requests that.
- Shared icons in OpenTracks v4.11.3:

  | Icon family | Distinct types sharing it |
  | --- | --- |
  | Bicycle | biking, cycling, road biking, track cycling |
  | Mountain bike / motorbike | mountain biking, dirt bike, motor bike |
  | Runner | running, street running, track running, trail running |
  | Walker / hiker | walking, hiking, off trail hiking, trail hiking, speed walking |
  | Skier | skiing, cross-country skiing |
  | Boat | boat, ferry, motor boating, RC boat |
  | Airplane | airplane, commercial airplane, RC airplane, seaplane |
  | Car | driving, driving car, driving bus, ATV |

  Many other types (including rowing, paddling, skating and custom/unrecognized
  types) share the default icon. Never identify them from that icon. Ordinary
  swimming and swimming in open water have different icons but both can match
  `swimming`; keep their exact type labels separate. Small or unclear icons
  require explicit type evidence, not a visual guess.
- To resolve ambiguous candidates efficiently, first restrict selection to rows
  whose dates qualify: long-press one row, tap additional qualifying rows, then
  use the **selection menu > Aggregated stats**. This groups only selected
  records by explicit type name, with counts and totals. Read the exact target
  group, excluding title/description-only matches in other groups. `Select all`
  is safe only after all search results' dates have been verified in range.
- The ordinary bottom-left `aggregated_stats_button` opens all-record statistics;
  it does **not** inherit the search query. Do not confuse it with selected-row
  statistics. Another way to verify one ambiguous type is the observed `Edit`
  entry (selection menu, or detail > More options): read `Activity type`, then
  Cancel without changing or saving anything. The detail Stats page exposes a
  full start date/time, but need not show the exact type name.

#### Verify dates

- Use the task/device's current date, not the host date. Calculate the requested
  interval as explicit calendar dates before counting; a Monday-start calendar
  week is different from the last seven days. Do not equate a weekday label
  with membership in the requested week.
- In v4.11.3, Today/Yesterday are relative to the device date; past dates two to
  six days ago display only a weekday. Older dates display day/month (and year
  when different). Bare weekdays can also appear for future dates in this
  version, so do not assume every weekday is the most recent past occurrence.
  Results are newest first. Use an established full-date/Today anchor and order
  to bound dates; if a candidate's week/year or future/past status remains
  ambiguous, open its detail and read the full start date before including it.
  Reuse that anchor only where the observed ordering proves the date range.
- For counting, keep the search-and-verify route above rather than opening the
  date filter dialog. For other queries that require the full-library date
  filter, budget both endpoints and Apply before entering it. In that filter,
  tapping `From` or `To` expands a calendar below the date label.
  Scroll the calendar into view if needed; the date label is not a text-entry
  shortcut. Check both endpoints rather than assuming the other date is correct.
  When the expanded calendar is below the viewport, use a deliberate coordinate
  `swipe` within the filter form to reveal it; repeated short `scroll` actions
  can waste the query budget. Once the needed date cells are visible, select
  from the current view rather than continuing to scroll.

#### Record an activity

- Tap the bottom-right `Record` control on the main screen to start recording.
  Long-press the recording control to end the recording.
- After recording ends, edit the record fields as required. Set `Name`, choose
  the exact `Activity type` from its selector, and enter `Description` when the
  task supplies one; then save with the confirmation control.

Use the current UI state for task-specific values and dates. This skill supplies
no task-instance records, date ranges, counts, totals, or success verdicts.
