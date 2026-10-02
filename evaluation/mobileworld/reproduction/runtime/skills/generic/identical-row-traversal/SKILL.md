---
name: identical-row-traversal
description: Track distinct occurrences through visually indistinguishable rows in a vertical single-column list. Not for grids or lists with clear per-row anchors.
version: 1.0.0
interface_scope: generic
kind: generic
role_sections: true
tags: [list, traversal, identical-rows]
source: authored
---

# Identical row traversal

## Shared

### Hints

- Use only when visually indistinguishable rows fill the usable view or continue
  at its bottom in a vertical single-column list, and each row fits the viewport.
  For grids, masonry, horizontal lists or oversized items, use layout-appropriate
  anchors instead. Similar titles with distinguishable visible fields alone do
  not trigger this procedure.

This skill preserves occurrence tracking within an ambiguous segment; it does
not define record equality or which fields the task requires. Keep coverage
and comparison requirements scoped to the requested target, group or range.
Use `adaptive-list-traversal` for ordinary traversal outside this segment.

## Execution

Enter this cycle when visually indistinguishable rows fill the usable view or
continue at its bottom in a vertical single-column list, and each row can fit
inside the viewport. Do not wait for proof that more rows exist off-screen.
Finishing the currently visible rows does not establish the segment's end.
The next move must still be a small reveal of the immediately following row,
not a large swipe to search for an end or a new anchor. It supports reading,
counting or other requested work; it does not require opening each item.
Never reverse-scroll or reopen a processed row to recover identity here.

Estimate local row size from the current screenshot; do not assume uniform
heights across the list.

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

Apply the task's stopping condition. A target lookup can stop when the
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

