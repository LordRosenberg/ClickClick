---
name: markor-file-management
description: Markor note save, rename, and delete workflow.
version: 1.1.9
app_aliases: [Markor]
role_sections: true
app: net.gsantner.markor
interface_scope: app
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
- When the requested filename includes a suffix, enter the base name in the left
  Name field and the suffix in the right Name field; do not also change `Type`.
- For a suffixless filename, enter the name and set `Type` to `None`. Do not
  clear the suffix first; ignore the light-gray suffix placeholder afterward.
- When a task requires clipboard content, paste it at the intended insertion point.
  Do not type temporary content and then try to select or overwrite it to perform
  the paste.
- After creating or editing a document, confirm the intended editor content and
  explicitly Save. With the filename and location already established and no
  save error, finish that work in the editor; reopening adds no required check.
  For renaming, save and return to the file list: Rename is not in the editor
  title, overflow menu, or File settings.
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
