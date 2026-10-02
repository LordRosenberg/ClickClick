---
name: fossify-calendar-create-events
description: "Create a timed batch from week slots, date-only entries from month days, and isolated distant events in the editor."
version: 1.0.13
app: org.fossify.calendar
interface_scope: app
kind: workflow
capability: create_calendar_events
tags: [calendar, events, scheduling, duplicate]
source: authored
---

# Create events in Fossify Calendar

## Procedure

Choose one route before opening an editor:

- **Same date and times:** For several events with identical dates and start/end
  times, create and save the first, then use it for the remaining events. A
  known existing event with those same values can also be the starting point.
  To open the saved event: close any search; in Month view, tap its date to
  open the day's list, then open its title → `Duplicate event`. Month labels
  may be clipped; the day list shows full titles. Edit the differing fields
  (including reminders/recurrence), leave matching dates/times, and save.
  Repeat from the saved event for another matching event. If no suitable
  saved event is known, use the new-event route below.
- **Otherwise, create a new event:**
  - Several timed events in the same week: before creating the first event,
    switch from the current view to `Weekly` via `Change view` (grid icon),
    then navigate to that week. For each event, `double_tap` the
    empty cell at its date and start hour → `Event` → enter the title → save.
    Tap between the hour lines, not on the labelled line. The cell prefills
    the start hour and a one-hour duration; adjust minutes or end time only
    when different. Keep Weekly open for the next event. Month-view long-press
    fills only the date, leaving the times to be set manually.
  - A single distant event or dates far apart: use `New event` and its date picker.
  - Date-only entries: in Month view, `long_press` the day and choose `Event`.
  Switch to Weekly before navigating: changing the view resets its date.
  A single-tap `+` expires after about five seconds; double-tap the cell instead.

If a duplicate's schedule must change, set its new start date/time first:
the end moves with it, preserving duration. Adjust the end only if the
duration differs.

## Verification

### Hints

- Use the editor's displayed start/end values and recurrence to confirm the
  requested schedule before saving. A time-grid coordinate is only meaningful
  for the currently visible date and time labels.
