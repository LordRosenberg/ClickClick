---
name: androidworld-expense-transfer
description: AndroidWorld ExpenseAddMultipleFromMarkor source annotation convention; evaluation profile only.
version: 1.3.0
app_aliases: [Expense, Pro Expense]
role_sections: true
app: com.arduia.expense
kind: app_core
capability: expense_transfer
source: authored
---

# AndroidWorld reimbursable expense transfer

## Shared

### Hints

- Only in the AndroidWorld evaluation profile, for transferring reimbursable entries
from Markor: the terminal `. Reimbursable.` added to a source note is a selection
annotation, not part of the destination expense note. Copy the underlying note
without that exact terminal annotation. Preserve all other source content and
existing expenses. An explicit request to retain the annotation takes precedence.
- Remove the suffix including its first period: `memo. Reimbursable.` becomes
`memo`; `memo.. Reimbursable.` becomes `memo.`. Keep the note's own punctuation.

This is a benchmark convention, not a general rule for expense notes. Do not apply
it to ordinary user tasks or other fields. Read the actual source entries; this
skill supplies no target names, amounts, private fixtures or success verdict.
