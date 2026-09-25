---
name: documentsui-location-evidence
description: DocumentsUI navigation and directory-membership evidence, including the non-directory-scoped behavior of file search.
version: 1.0.1
app_aliases: [Files, DocumentsUI]
role_sections: true
app: com.google.android.documentsui
interface_scope: system
device_profiles: [androidworld_api33]
kind: app_core
capability: navigate_and_verify_file_locations
source: authored
---

# DocumentsUI location evidence

## Shared

### Hints

- Search may locate a candidate file, but verify its parent directory only in an
  ordinary directory listing: exit search, navigate to the exact directory, and
  confirm its breadcrumb or folder context before checking the row.

### Anti-patterns

- Do not treat DocumentsUI search as scoped to the directory visible when search
  was opened. Finding a file after entering a folder such as `DCIM` does not prove
  that the file belongs to that folder. So, do not use search to verify whether a file was moved successfully.

This skill supplies no filenames, source paths, destination paths, or success
verdicts. Explicit user instructions take precedence.
