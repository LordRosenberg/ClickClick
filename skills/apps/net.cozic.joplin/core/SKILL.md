---
name: joplin-notebook-navigation
description: Navigate Joplin notebooks and note lists when the accessibility tree exposes a closed drawer or a background toolbar.
version: 1.0.0
app_aliases: [Joplin]
role_sections: true
app: net.cozic.joplin
interface_scope: app
kind: app_core
capability: navigate_notebooks
source: authored
---

# Joplin notebook navigation

## Shared

- Joplin can expose notebook names and a background Sidebar control in the tree
  while the drawer is closed. Their indexed boxes may overlap visible notes;
  a notebook name in the tree does not mean its row is currently on screen.
- From the visible note list, open its upper-left drawer button, then select the
  requested notebook only after its drawer row is visible in the screenshot.
  Confirm the resulting list title before counting or selecting notes.
- In an open note, use its visible Back arrow to return to the note list before
  opening the drawer. Ignore background Sidebar nodes on that screen. If an
  indexed target disagrees with the screenshot, use the visible control's actual
  position; do not substitute a guessed position for a hidden notebook row.
