---
name: simple-calendar-core
description: Read complete event titles from Simple Calendar Pro's date lists instead of clipped month cells.
version: 1.0.1
app_aliases: [Simple Calendar Pro, Simple Calendar]
role_sections: true
app: com.simplemobiletools.calendar.pro
kind: app_core
capability: read_calendar_events
source: authored
---

# Simple Calendar event titles

## Shared

### Hints

- Month-grid event labels can be clipped without an ellipsis. Use that grid to
  locate relevant dates, not as proof of a complete event title. When the task
  asks for titles, tap each relevant date containing events and read its event
  list. If that list also clips a title, open the event to resolve it. Reuse
  already verified full titles; no need to reopen them.
- Opening a date displays its day list. The accessibility tree can retain hidden
  month-grid nodes behind that page: numbered boxes alone do not prove that the
  calendar is visible or tappable. Use the actual displayed date numbers, not
  retained `month_view_background` nodes. Use Back to return to the month, or
  the day heading's arrows for adjacent dates. If inspecting a contiguous range
  day by day, start with its first date to avoid backtracking. Read the selected
  date heading before associating events with that date.
