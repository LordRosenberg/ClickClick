---
name: adaptive-list-traversal
description: Traverse multi-screen lists to find, read, count or process records. Adapt scrolling to visible anchors and preserve coverage; use the bottom-complete-row cycle only for suitable single-column lists.
version: 1.0.12
kind: generic
tags: [list, traversal, scrolling, coverage]
source: authored
---

# Adaptive list traversal

## Scope

Use for multi-screen record lists. Establish the requested scope: finding a
specific target, processing a group/range, or covering the whole collection.
Use an available search, filter or aggregate when it preserves that scope and
saves work. Finding a target does not require inventorying unrelated records.
This skill does not define item equality, authorize mutations, or govern form
scrolling, calendar grids, maps or video seeking.

## Hints

- Use a coordinate `swipe` when controlling travel distance, with both endpoints
  inside the usable list. The `scroll` action uses a fixed short gesture;
  increasing `duration_ms` does not lengthen that gesture. With clear anchors,
  prefer a swipe across roughly half to two-thirds of the usable list height
  over repeated short scrolls; avoid an edge-to-edge fling.
- Clear anchors are distinguishable row labels, date/group headers or other
  visible landmarks whose order connects successive views. With clear anchors,
  use larger controlled swipes inside the list, retaining enough overlap to
  account for the transition. An established ordering can justify passing a
  region outside the requested scope; a full inventory still needs coverage of
  the intervening rows.
- Clipped labels, uncertain overlap or a nearby target/range boundary call for
  smaller moves. For visually indistinguishable rows, use the single-direction
  procedure below when its layout conditions hold.
- A current SOM index or screen position identifies a current action target,
  not a persistent record. Match overlap using visible content and relative
  order; identical labels alone do not identify an occurrence.

## Identical-looking rows: advance the bottom complete row

Enter this cycle when visually indistinguishable rows fill the usable view or
continue at its bottom in a vertical single-column list, and each row can fit
inside the viewport. Do not wait for proof that more rows exist off-screen.
Finishing the currently visible rows does not establish the segment's end.
The next move must still be a small reveal of the immediately following row,
not a large swipe to search for an end or a new anchor. It supports reading,
counting or other requested work; it does not require opening each item.
Never reverse-scroll or reopen a processed row to recover identity here.

For grids, masonry layouts, horizontal lists or items taller than the viewport,
use layout-appropriate anchors and the coverage rules below instead. Do not
wait for an oversized item to become wholly visible. Estimate local row size
from the current screenshot; do not assume uniform heights across the list.

Judge row completeness **from the current screenshot only**, using the App's
actual row layout. Confirm that the whole item is visible inside the usable
list, without top or bottom clipping. Use whatever visual cues that layout
provides, such as a card edge, divider, background transition, or the start of
the next item; do not require whitespace, a border, or any one cue in every App.
Fully displayed text alone is insufficient. A tree node, its text, bounds, or
SoM box does not prove visual completeness. Use the tree/current index to locate
a click target only after the screenshot establishes completeness. If it remains
uncertain, keep that row pending.

1. Process the current unprocessed **fully visible rows** from top to bottom.
   The lowest complete row, once processed, is the last processed boundary.
   Keep a short cursor note with its top/bottom edges, local row size, processed
   count, and the immediately following pending row. A row cut off by the
   list's bottom edge is pending: do not open it, count it, or discard it from
   coverage. Processing means completing the task's required read, count or
   action for that row, not automatically opening its details.
2. Fix the immediately following pending row as the target of this reveal.
   Keep that target until it is processed; do not redefine it as whichever row
   is currently lowest on screen. Make one slow forward coordinate drag with
   limited inertia, sized from the screenshot to reveal this target. A move
   approaching one local row height is appropriate when needed; do not impose
   repeated half-row moves regardless of the remaining gap. Observe next.
3. **After every drag, check the lowest complete row before considering another
   drag.** The previous pending row may now be complete, with a NEW clipped row
   below it. Process that newly complete row first, even though a half-row is
   still visible below. Only after processing it does that lower half-row become
   the next reveal target. Do not chase successive bottom half-rows without
   processing the complete rows immediately above them.

   Continue a smaller finishing drag only when the ORIGINAL reveal target is
   still clipped and the lowest complete row is established as the already
   processed boundary. The presence of any clipped bottom row is insufficient.
   Identical text or a nearly unchanged lowest-row position does not establish
   this: a one-row move can put a different occurrence at almost the same place.
   If correspondence is uncertain, retain the gap; do not assume a failed move,
   increase the gesture, or reopen the old row to resolve it. Gesture endpoints
   alone do not establish correspondence either.

   Before the next action, keep the cursor explicit: last processed row,
   fixed pending row, and either "process newly complete pending row" or
   "finish revealing the same pending row". Advance the processed count and
   pending target only after the required work, never merely after a swipe.
   Use the screenshot for completeness and the current frame's target/index
   for any required action; never reuse a previous frame's SOM index.
4. Repeat one new complete bottom row at a time. The processed count is a
   lower bound, not the group's assumed total. If a move unexpectedly exposes
   several rows or leaves their correspondence uncertain, do not skip straight
   to the lowest one or reinterpret the count as proof of coverage.

5. **Evaluate exit only AFTER processing the fixed pending row, not immediately
   after the drag.** A clearly different next row may first appear BELOW the
   newly complete pending row. Process that last pending row before changing
   scope or reporting completion; the different row does not replace the reveal
   target. The same order applies when reaching the actual list bottom.
   Exit only after the screenshot shows a clearly distinguishable next row or
   the actual bottom is confirmed, and every preceding in-scope pending row is
   processed. Then resume normal sizing with the distinct next row as an anchor,
   or apply the task's stopping condition at the bottom. Different detail values,
   a processed screen, or an assumed group size do not permit exit. Similar rows
   remaining after a move are not automatically old overlap.

Apply the task's stopping condition below. A target lookup can stop when the
requested target is established. For group/range or whole-list coverage, use
an observed task-relevant boundary or the actual list end, with every preceding
in-scope row processed.
A label change is a boundary only if the task and the observed ordering justify
it. An unchanged number of visible similar-looking rows does not mean they are
the same occurrences: new rows enter as old rows leave.
Do not enlarge the gesture merely because the current viewport is processed.
If the positional correspondence is ambiguous or the occurrence number jumps,
keep that coverage gap explicit; changing a count does not inspect a skipped
row. Resume larger swipes only outside the ambiguous group.

## Account for each new view

1. Reconcile overlap with the preceding view before counting new records. Keep a
   compact account of processed rows and pending rows relevant to the task.
   A row is processed when the task's required fields or action result have been
   established; opening every detail is unnecessary if the visible fields suffice.
2. Resolve visible pending rows now, before scrolling ahead to find the group
   end or count its members. Coverage grows as rows are processed; knowing the
   final group size is not a prerequisite. Scroll only as needed to reveal a
   pending row or, when the current rows are resolved, reach the next rows.
   In the identical-looking-row cycle, reveal a clipped bottom row completely
   before processing it. It remains pending until then. A newly revealed row
   with uncertain identity/status also remains pending. Do not reprocess known
   overlap merely because its index changed.
3. Only after pending rows are resolved, evaluate the stopping condition below.
   Otherwise continue within the current group/range, even if a following group
   is already visible. After a deletion, insertion or reorder, re-anchor from
   the changed list before deciding which occurrence is next. A viewport that
   remains full after deletion can be backfilled with earlier or already
   processed rows; neither its row count nor a reused position proves a new
   occurrence. Content equality alone does not establish occurrence identity.
4. A changed search or filter does not prove the list returned to its beginning.
   Establish the start of the requested scope and account for any unresolved
   rows above the viewport before claiming that the filtered scope is covered.

## Stopping conditions

- **Target lookup:** stop when the requested target is identified and the required
  work on it is supported; no whole-list scan is implied.
- **Group/range coverage:** require both its observed boundary and no pending
  in-scope rows through that boundary. Explicitly account for the last relevant
  row before that boundary. Use headers or labels only when the task's grouping
  or ordering makes them valid boundaries; their appearance alone does not
  establish that preceding rows were processed.
- **Whole-list coverage:** require an established list end and no unresolved
  coverage gaps. An unchanged view alone is ambiguous while loading, after a
  missed gesture or with unreliable observations. Resolve that local ambiguity
  using current evidence rather than repeatedly issuing the same scroll.

For changing or unbounded feeds, use the requested range or stopping target;
do not invent an exhaustive scan or wait indefinitely for the page to stop
changing. Keep any remaining coverage uncertainty explicit.
