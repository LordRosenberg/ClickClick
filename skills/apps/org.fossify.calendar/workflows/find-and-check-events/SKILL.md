---
name: fossify-calendar-find-and-check-events
description: "Find calendar events for named items or resources; check dates and booking overlaps when titles differ."
version: 1.0.1
app: org.fossify.calendar
interface_scope: app
kind: workflow
capability: find_and_check_calendar_events
tags: [calendar, search, dates, conflicts]
source: authored
---

# Find and check calendar events

## Procedure

1. Search using the distinctive item or resource name. If the full title has
   no match, shorten it to its identifying words or inspect the relevant date
   in `Simple event list`. Calendar titles can use different activity words;
   one empty exact-title search does not establish that no event exists.
2. Match candidates by the named item/resource and relevant date, using the
   full title and start/end values. For booking conflicts, compare intervals
   for the same resource; touching endpoints alone are not an overlap.
3. For a timeline accuracy check, compare the calendar milestone with the
   completion/target date in the update. A "within N days" tolerance applies
   to that claimed date unless the user specifies another reference. For
   other relative-date requests, use their stated reference; if unclear,
   preserve the uncertainty instead of silently substituting today.

## Verification

- Reuse a matched event's visible title and times once sufficient for the
  requested check. Open details only for missing or ambiguous fields. Keep
  an incomplete search distinct from a confirmed absence in the checked range.
