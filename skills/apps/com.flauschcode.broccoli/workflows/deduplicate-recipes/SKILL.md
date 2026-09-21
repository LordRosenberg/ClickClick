---
name: broccoli-deduplicate-recipes
description: Compare same-title recipe groups in Broccoli and remove exact duplicates. Pair with adaptive-list-traversal for multi-screen group coverage.
version: 0.3.5
app: com.flauschcode.broccoli
kind: workflow
capability: recipe_deduplication
tags: [recipes, deduplication]
source: authored
verified_actions:
  - id: broccoli.inspect_recipe_and_return
    template: tap_capture_key
    key: back
    purpose: Open the targeted recipe, preserve its detail evidence, and return to the recipe list.
---

# Deduplicate recipes

## Hints

- `All recipes` is ordered by title, so same-title records are contiguous.
- The tested recipe list uses vertical cards that fit within the viewport.
  A slow drag around 600–800ms is a starting point for revealing the next card;
  actual movement and card completeness still require screenshot confirmation.
- In stage plans, visible same-title counts are lower bounds until coverage is
  complete. Require inspection of the LAST pending same-title card before the
  different-title card, followed by any verified surplus deletion. Do not end
  a stage merely at "next title observed" or at a guessed candidate count.

## Procedure

1. Use `All recipes` for collection-wide deduplication. Pair this workflow with `adaptive-list-traversal` for scroll sizing, row accounting and group boundaries. Each same-title group is the comparison scope; matching card fields make individual occurrences ambiguous, so process those candidates in list order.

   When matching cards fill the usable list or continue at its bottom, enter
   the shared bottom-complete-row cycle immediately; more off-screen matches
   need not already be proven. Keep small reveals active until the screenshot
   shows a clearly different next card (title or visible comparison fields),
   or the actual list bottom is confirmed, with every preceding pending card
   processed. A visually different card can end the ambiguous segment without
   ending a same-title comparison group. Distinct detail signatures for every
   currently visible card do not establish the group's end or permit a large boundary-
   seeking swipe. No advance group count or search of every group is needed.
   Different detail signatures do not create visible anchors on identical list
   cards. In this title-ordered list,
   the next different title identifies a group boundary only after the last
   preceding same-title occurrence and all earlier pending candidates have been
   processed; the final group instead ends at the established list end.
   If that different title first appears during a reveal, inspect the fixed
   pending same-title card above it before ending or handing off the group.

2. Compare the description, source, and image shown on the cards first. If any of these fields differ, the records are not exact duplicates and do not need detail inspection.

3. For candidates whose card fields match, compare servings, preparation time, ingredients, directions, favorite state, and the other visible details. Use `broccoli.inspect_recipe_and_return` for every required detail inspection; do not replace it with separate open and Back actions unless the authorized action is unavailable or reports failure. Its returned historical detail evidence is retained in the Executor dialogue for later comparisons. With more than two candidates, record each complete field signature in a compact note before inspecting the next candidate, then group records by those signatures instead of repeatedly reopening a pair.

   Keep these comparison signatures separate from the shared traversal cursor;
   equal signatures alone do not prove that two distinct occurrences were inspected.

4. Once the group's coverage is established, keep one record from each exact-duplicate signature and delete the extras. For a signature verified in N distinct occurrences, delete at most N-1 and update that remaining count after each confirmed deletion. A retained signature in historical evidence does not prove that a separate retained record still exists: after list reflow, the current matching row may be that retained record. Resolve its occurrence identity before considering another deletion. Resolve that group's comparisons and deletions before moving to another title group.

## Verification

- Use the established group coverage, collected signatures and observed deletions to verify that one record remains per exact-duplicate signature. Do not reopen an inspected record to resolve an identity gap or repeat a comparison. Opening an already-verified surplus occurrence to perform its deletion is a mutation step, not a new inspection or evidence of another occurrence. Keep unresolved coverage gaps explicit rather than declaring the group complete.
