---
name: opendocument-reader-pdf-navigation
description: Read PDFs at a legible scale and search within rendered sections in OpenDocument Reader.
version: 1.0.4
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

- Keep the overview scale for search and page/section navigation. Before reading
  a target passage or table with broad gray gutters on both sides, double-tap
  a plain area near it, away from links, to bring the page approximately to
  screen width; pan if an edge is clipped. Skip zooming when already at page width or closer. Use the
  screenshot for text or table relationships missing from the tree; page width
  does not guarantee every small cell is legible. Return to the overview only
  when needed for navigation.
- Search may only reach nearby rendered pages, not the whole PDF. Navigate by
  page numbers or section headings, then search within the relevant section.
  Prefer a distinctive single word: PDF text spacing can break phrase matches.
  Use Next while it reveals new relevant passages. If it stops moving or repeats
  passages, close search, swipe to another section, and search there. No match
  does not establish absence from the document; query variants on the same
  pages do not extend coverage.
