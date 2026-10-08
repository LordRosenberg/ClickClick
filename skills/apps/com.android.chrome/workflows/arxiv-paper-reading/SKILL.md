---
name: chrome-arxiv-paper-reading
description: Read arXiv papers in Chrome; use the matching PDF when required content is missing from the HTML representation.
version: 1.0.0
app: com.android.chrome
interface_scope: app
kind: workflow
capability: arxiv_paper_reading
tags: [arxiv, paper, html, pdf]
source: authored
---

# arXiv paper reading

## Procedure

### Hints

- On arXiv paper pages, HTML conversion may omit or misrender content. If a needed
  table, formula, or passage is missing from the relevant HTML section, consult
  the PDF of the same paper and version.

## Verification

### Hints

- An HTML search miss alone does not show that the paper lacks the content.
  This fallback does not require PDF verification when the available evidence
  already satisfies the task.
