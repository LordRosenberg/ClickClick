---
name: markor-file-management
description: Markor note save, rename, and delete workflow.
version: 1.1.8
app_aliases: [Markor]
role_sections: true
app: net.gsantner.markor
kind: app_core
capability: manage_notes
source: authored
---

# Markor file management

## Shared

### Navigation

- In the file list, the upper-left toolbar title is the current folder name. The
  `..` row navigates to its parent; any path shown under `..` identifies that parent,
  not the current folder.
- To-Do and QuickNote are shortcuts to configurable files, normally in the
  notebook, not separate note collections merely because their tabs exist.
  Cover actual in-scope files and subfolders; check configured locations when
  they differ or the task requires them. `.app/snippets` contains editor snippets;
  do not delete support folders as a shortcut for deleting notes.

### Procedure

- When editing or reusing document text, preserve observed whitespace and line
  breaks outside the requested changes, including trailing newlines.
- When the requested filename includes a suffix, enter its base name in the left
  Name field and the suffix in the right Name field. Markor then recognizes the
  file type automatically; do not also change `Type`.
- When the requested filename has no suffix, leave the right Name field without
  an actual suffix and set `Type` to `None`. A light-gray default suffix is only
  a placeholder and can be ignored; a dark suffix is actual input and must be
  cleared, or `None` will not produce a suffixless file.
- When a task requires clipboard content, paste it at the intended insertion point.
  Do not type temporary content and then try to select or overwrite it to perform
  the paste.
- After creating or editing a document, explicitly save its content; visible
  editor text alone does not confirm that it reached the file. A creation-only
  task can finish in the editor after saving; returning to the list is not an
  extra requirement. For renaming, save and return to the file list: Rename is
  not in the editor title, overflow menu, or File settings.
- In the file list, long-press the exact filename, confirm its selection, choose
  top-toolbar Rename, enter the exact requested name, and confirm.
- Delete from the file list. For multiple target files in one folder, long-press
  the first, then tap other target rows to add them to the selection; verify each
  row is marked Selected. Use top-toolbar Delete once, check the confirmation's
  filenames, and confirm once. Keep parent rows, support folders and non-targets
  unselected. A single-file deletion uses the same flow with one selected file.

### Verification

- Verify the renamed entry is present or the deleted entry is absent; saving or a
  selection highlight alone is insufficient. Do not add reopening the file as a
  verification step unless the task explicitly asks to reopen it.

### Anti-patterns

- Do not probe editor controls for rename/delete, select an entire folder to save
  steps, or treat an unverified selection highlight as a completed deletion.

This skill supplies no target filenames, note contents, or success verdicts.
Explicit user instructions take precedence.
