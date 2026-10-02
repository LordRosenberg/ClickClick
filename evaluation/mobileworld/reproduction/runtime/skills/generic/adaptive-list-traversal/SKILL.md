---
name: adaptive-list-traversal
description: Traverse lists or grids using visible anchors, date groups and scope boundaries. Select identical-row-traversal separately for indistinguishable single-column rows.
version: 1.1.0
interface_scope: generic
kind: generic
role_sections: true
tags: [list, traversal, scrolling, coverage]
source: authored
---

# Adaptive list traversal

## Shared

### Hints

- Use for finding a target, processing a group/range, or covering a collection
  across screens. Search, filters or aggregates can reduce traversal when they
  preserve the requested scope. This skill does not govern form scrolling,
  calendar grids, maps or video seeking.

Coverage means establishing the task's required fields or action results for
the relevant items. It does not require opening every detail, collecting extra
identifiers, or completing a separate inventory before acting. App guidance
owns ordering, field comparisons and mutation semantics.

Select or `load_skill` `identical-row-traversal` when visually indistinguishable rows
fill a vertical single-column view or continue at its bottom, and each row fits
the viewport. A grid or list with distinguishable anchors does not need that
procedure.

## Execution

### Hints

- Use coordinate `swipe` endpoints to control distance. `scroll` uses a fixed
  short gesture; increasing `duration_ms` does not lengthen it. With clear
  anchors, prefer roughly half to two-thirds of the usable list height,
  retaining enough overlap to connect successive views; avoid edge-to-edge flings.
- Date/group headers, distinguishable labels and relative order can connect
  views. Near a target/range boundary or with clipped labels or uncertain
  overlap, use smaller moves. Do not wait for oversized items to fit wholly.
- Current SOM indices and positions are action targets, not persistent record
  identifiers. Reconcile overlap using visible content and order; an index
  change alone does not make an item new. Retain only progress needed to avoid
  omissions or duplicate work; visible fields may suffice without detail views.
- Resolve relevant pending items before scrolling past their boundary. After
  deletion, insertion or reordering, re-anchor from the changed list: backfilled
  rows may already have been processed. A changed filter does not establish
  that the list returned to its beginning.
- For target lookup, stop once the target and required work are established.
  For group/range coverage, establish its boundary and account for in-scope
  items through it. A following header alone does not mean preceding items
  were processed. Whole-collection coverage also needs an established list end.
- Observed ordering may exclude regions outside the requested scope. For
  changing or unbounded feeds, stop at the requested range or target instead
  of inventing an exhaustive scan. An unchanged view during loading or after
  a missed gesture does not establish the end; resolve the local uncertainty
  rather than repeatedly issuing the same scroll.
