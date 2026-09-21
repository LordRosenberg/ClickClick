---
name: androidworld-settings-copy-to-clipboard
description: Copy exact text to the clipboard via Settings search when the task names no editing App.
version: 1.0.0
app: com.android.settings
device_profiles: [androidworld_api33]
kind: workflow
capability: copy_text_to_clipboard
tags: [settings, search, clipboard, copy, text-selection]
source: authored
---

# Copy text through Settings search

## Procedure

1. Launch Settings directly, open its search field, and use `replace_text` to
   enter the exact requested text. Do not submit the search.
2. Long-press inside the entered text. Continue only after selection handles and
   the native text-action menu are visible.
3. If the whole requested string is not selected, open the text-action menu's
   overflow and choose `Select all`. Reacquire current indices after the menu
   changes, then choose `Copy`.

Use the visible selection menu rather than Chrome's omnibox or keyboard
shortcuts. ADB Keyboard being active does not prevent native text selection.

## Verification

Before choosing `Copy`, verify that the selected text covers the entire exact
payload, including spaces and punctuation. A dispatched long press without
visible selection handles is not evidence that text was selected.
