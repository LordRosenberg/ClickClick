---
name: opendocument-reader-pdf-navigation
description: Read PDFs and count numbered plain-text lines in OpenDocument Reader.
version: 1.0.27
app_aliases: [OpenDocument Reader]
app: at.tomtasche.reader
interface_scope: app
kind: app_core
role_sections: true
source: authored
---

# OpenDocument Reader PDFs

## Execution

### Hints

- Keep the overview scale for navigation to a known target location. Before reading
  a target passage or table with broad gray gutters on both sides, double-tap
  a plain area near it, away from links, to bring the page approximately to
  screen width; pan if an edge is clipped. Skip zooming when already at page width or closer. Use the
  screenshot for text or table relationships missing from the tree; page width
  does not guarantee every small cell is legible. Return to the overview only
  when needed for navigation.
- PDF page-node IDs `pf...` encode hexadecimal file-page numbers (`pf10` = 16),
  not printed page labels. Adjacent page nodes may be off-screen; confirm the
  visible page from the screenshot before reversing direction.
- PDF search covers only two pages before and after the current position,
  not the whole document. When the target location is unknown, scan successive
  views rather than relying on search. After a local search miss, close search and continue reading.
  Reading new pages extends coverage even without a match; a few misses do not
  rule out the unread remainder.
  Keep direction unless a document boundary, a lead visible in the document,
  or an identified coverage gap calls for a change; do not reopen search or
  switch readers because of the miss.
- Use large scrolls or jumps only when a supplied locator or an observed document
  cue establishes the target's location. Guessing that content is near the beginning
  or end does not establish its location. Confirm the landing and do not count skipped
  content as inspected. Otherwise scan in order with short, slow scrolls and overlapping
  visible content. Check actual movement and rendered content after each scroll; if
  the view is blank or loading, let it render before continuing. If unread content was
  skipped during scanning, reduce the scroll and cover the gap before continuing.
- In this app's numbered plain-text view, exclude one final row with no content:
  it is the trailing editor slot. Count preceding blank or whitespace-only lines;
  visual wrapping does not add a line. Confirm the file's end and use the
  screenshot for rows missing from the tree. An open error is not evidence of
  an empty file.
