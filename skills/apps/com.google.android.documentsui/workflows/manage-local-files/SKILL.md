---
name: documentsui-manage-local-files
description: 在 AndroidWorld API 33 的 Files/DocumentsUI 中打开和管理本地文件：HTML 必须长按选中后用打开方式选择 Chrome，TXT 可直接单击；支持移动、复制、排序和删除。
version: 0.5.1
app: com.google.android.documentsui
device_profiles: [androidworld_api33]
kind: workflow
capability: manage_local_files
tags: [files, open, html, txt, chrome, move, copy, sort, delete, directory]
source: authored
---

# Manage files in DocumentsUI

## Hints

- On `androidworld_api33`, open HTML by long-pressing the file, then choosing Open with > Chrome; open ordinary TXT files with a single tap on the filename or row outside selection mode.
- Confirm visible file contents before reporting that opening succeeded.

## Procedure

1. The title and breadcrumb identify the current listing. To open a clearly identified visible file, use that row directly; do not revisit the storage root merely to reconfirm the folder name. Establish the full path when files are missing or ambiguous, or for path-sensitive operations such as moving or creating. “显示根目录” opens the storage-root drawer, not the parent directory. “下载内容” is a provider view; if a known file is missing there, inspect internal/shared storage and its actual Download directory before concluding it is absent.
2. Choose the opening gesture by file type on this AndroidWorld API 33 device. For an HTML file (`.html` or `.htm`), long-press the intended file, confirm that exactly that file is selected, open the selection toolbar's overflow menu, choose “打开方式” / “Open with”, and select Chrome. HTML requires this selection-mode route in this environment; do not spend repeated taps trying to open it directly. For a plain text file (`.txt`), exit selection mode if necessary and single-tap its filename or row to open it. Long-press selects a file; selection alone does not open it. Use selection for move, copy, or delete operations as well. Sorting is a list-view menu action and does not require selecting a file.
3. Chrome first-run onboarding is conditional. If shown, follow the visible path: use `Accept & continue` followed by `No thanks` on `Turn on sync?`, or choose `Use without an account` when that variant is visible. Do not search for onboarding screens that are absent. If a transition is pending or the result is unclear, use a fresh observation before retrying the same visible prompt.
4. Completing Chrome onboarding can return to Files without opening the HTML page. In that case reopen the same HTML file through the selection-mode `Open with` > Chrome route. If the file remains the only selected item, reuse that selection; otherwise long-press the exact file again. Skip this second route when Chrome was already initialized or the requested page opened immediately.
5. “移至…” opens a destination picker. Navigate through the requested destination path inside that picker. Its bottom “移动” button commits into the currently displayed directory; it does not navigate to, infer, or automatically create the requested destination. Confirm the full destination breadcrumb before pressing it.
6. For deletion, select only the requested file and inspect the confirmation before confirming. Cancel an unexpected open-with dialog rather than selecting another app.

An empty provider listing is not proof that a physical folder is absent. Before
creating a missing destination, inspect the actual shared-storage path. Match each
path component exactly, including suffixes, when several similar folders appear.
If creation produces an automatically renamed folder such as `name (1)`, it is a
different destination; re-establish the requested path before committing the move.

## Verification

After opening HTML, confirm that Chrome displays the requested page content. Chrome onboarding completion, an app-picker choice, an input dispatch acknowledgement, or a selected-file highlight is not evidence that the file opened. Reacquire control indices after every screen transition. If Chrome returns to Files without loading the page, repeat the HTML open-with route using a fresh observation. After opening TXT, confirm that its contents are visible. This HTML-specific rule is verified for `androidworld_api33`; do not generalize it to other phone systems or file types.

After moving, verify the exact filename in the exact destination directory and its absence from the original directory. An empty source alone also fits a move to the wrong folder and is insufficient evidence. After deleting, verify the requested file is absent while unrelated files remain. Do not infer either outcome from a dispatched button press or a progress toast alone.
