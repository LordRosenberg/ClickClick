---
name: broccoli-delete-recipes-by-condition
description: Delete recipes matching field conditions using search and detail checks; not deduplication.
version: 1.1.1
app: com.flauschcode.broccoli
kind: workflow
capability: recipe_condition_deletion
source: authored
verified_actions:
  - id: broccoli.inspect_recipe_and_return
    template: tap_capture_key
    key: back
    purpose: Open the targeted recipe, preserve its detail evidence, and return to the recipe list.
---

# Delete recipes by condition

## Hints

- This procedure handles independent field matches. Selecting among duplicates
  requires the deduplication workflow's group comparisons instead.

## Procedure

1. For a positive text condition, first try a distinctive task keyword as a
   candidate search, even when the condition concerns directions: the same word
   may occur in indexed fields. For negative or numeric conditions, or no useful
   indexed term, use the relevant list instead.
2. Check each candidate's actual requested field. Delete a confirmed independent
   match as found; do not postpone every deletion until a full scan is complete.
   For read-only list scanning, prefer `broccoli.inspect_recipe_and_return`: it
   retains the details and returns to the list in one action. If inspecting a
   likely match to delete immediately, open normally to avoid returning and
   reopening. If the combined inspection reveals a match, reopen that same
   record from the current list to delete it; reuse the captured details.
3. After deletion, re-read the changed list and distinguish same-title records.
   Once search candidates are processed, inspect any remaining scope needed for
   collection-wide coverage.

## Verification

- Reuse the checked fields and observed deletion results. Claim completion only
  when the requested scope is covered; report remaining uncertainty otherwise.
