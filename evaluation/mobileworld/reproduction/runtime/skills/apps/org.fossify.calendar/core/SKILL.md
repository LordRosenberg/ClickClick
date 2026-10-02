---
name: fossify-calendar-core
description: Read Fossify Calendar events using date lists and event spans; distinguish search/day results from hidden month-grid nodes.
version: 1.0.2
app_aliases: [Fossify Calendar]
role_sections: true
app: org.fossify.calendar
interface_scope: app
kind: app_core
capability: read_calendar_events
source: authored
---

# Fossify Calendar events

## Shared

### Hints

- For events across a date range, `Change view` > `Simple event list` provides
  chronological entries. Scroll toward the requested dates; an initial view
  outside the range does not establish that earlier events are absent. Use
  event start/end dates to cover multi-day entries rather than reopening each
  covered day. If a span is unclear in the list, open that event.
- Search results and day lists can cover the month while its date cells
  (`month_view_background`) and New Event button (`calendar_fab`) remain in
  the tree. Use the screenshot's foreground controls; ignore covered month
  controls even if indexed as clickable. Close search or leave the day list
  before using the month. Use visible day-heading arrows for adjacent dates;
  read the heading before assigning dates. For a broad range, use the event list.
- Search can match locations and descriptions as well as titles. A keyword
  hit is a candidate: match the requested event type or resource in its actual
  field before including it in a count.
- Month-grid titles may be clipped without an ellipsis. Read the event list
  for complete titles, opening an event only if required fields remain unclear.
  Reuse established titles and spans rather than reopening them for each date.
