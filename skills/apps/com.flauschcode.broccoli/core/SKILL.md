---
name: broccoli-core
description: Broccoli searchable fields, query syntax and search-assisted recipe navigation, verified on version 1.2.6.
version: 1.4.2
app_aliases: [Broccoli]
role_sections: true
app: com.flauschcode.broccoli
interface_scope: app
kind: app_core
capability: recipe_search
source: authored
---

# Broccoli search (1.2.6)

## Shared

### Hints
- Search indexes title, description, source and ingredients, but not directions,
  servings or preparation time. Use indexed words to locate candidates; inspect
  the requested field to decide whether each candidate actually matches.
  For a positive text condition on directions, still try its keyword as a
  candidate search first: it may also occur in indexed fields. "Not indexed"
  means search cannot prove complete coverage, not that candidate search should
  be skipped. Verify directions before deleting each search candidate.
- Search may be scoped to a category or filtered collection. Check the active
  scope before treating results as collection-wide.
- Search uses SQLite FTS syntax and adds a trailing `*`. A hyphen in an unquoted
  title can exclude a term. If an exact title unexpectedly returns no results,
  try a distinctive plain word or a quoted phrase, then inspect candidate identity.
- Same-title recipes may be distinct records. Search narrows candidates; detail
  contents and task intent determine which occurrence to modify or delete.
- These mechanics are version-specific. If the observed app behaves differently,
  investigate that discrepancy instead of assuming this guidance proves a result.
