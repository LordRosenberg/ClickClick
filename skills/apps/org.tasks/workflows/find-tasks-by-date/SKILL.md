---
name: androidworld-tasks-find-by-date
description: Find Tasks.org items by due date or completion state in My Tasks. Pair with adaptive-list-traversal for multi-screen date-range coverage.
version: 1.3.2
app: org.tasks
device_profiles: [androidworld_api33]
kind: workflow
capability: find_tasks_by_date
tags: [tasks, due-date, completed, incomplete, next-week, information-retrieval]
source: authored
---

# Find tasks by date

## Procedure

1. Use the default `My Tasks` aggregate as the comprehensive date-query scope;
   do not open navigation merely to look for a separate `All tasks` list. Only
   switch lists when the current title visibly shows `Today` or another named
   filtered list, because sorting does not widen that filter.
2. Ascending due-date grouping helps locate date ranges. If needed, configure it
   through the lower-right sorting control: `Grouping` > `By due date`.
   Keep suitable existing settings. For queries that include completed tasks or
   do not restrict completion state, turn off `Move completed tasks to bottom`
   and confirm it is unchecked before closing the sorting panel. Selecting
   `By due date` alone leaves completed items in a separate section, sorted by
   completion time rather than due date. Its weekday labels cannot establish
   the requested week.
   Date-group headers can be tapped to collapse or expand that group. Collapse
   `Overdue` only after the request establishes a future date or interval.
   A weekday alone does not mean next week: for a completed-task query with no
   future qualifier, inspect matching weekday rows in `Overdue` before choosing
   a later `Due <weekday>` group. If the week remains ambiguous, open a matching
   row's due-date field to verify its calendar date instead of silently shifting
   the request to the next occurrence. Anchor relative
   weekday labels to `Today`, `Tomorrow`, or an explicit date before interpreting
   their week; `Mon` or `Tue` alone can describe a past date.
3. Pair with `adaptive-list-traversal` for multi-screen date queries. Ascending
   date headers locate the requested range and distinguish irrelevant groups;
   the first later date outside the range identifies its lower boundary.
   For the final date group, the established list end supplies that boundary.
4. Read all relevant rows in the requested date group or range using the shared
   coverage rule, including the last relevant task before the boundary.
5. Use each row's visible completion state: completed-only questions include
   checked rows, incomplete-only questions include unchecked rows, and queries
   without a completion restriction include both within the requested dates.
   Count or report titles exactly as requested; opening each task is
   unnecessary when its title and state are already visible.

## Verification

Confirm the current list scope and the visible due-date group headers. A title
seen under a different date group, or a task hidden by the current list filter,
does not answer the requested date or interval.
