---
name: photo-text-reading
description: Transcribe exact text or numbers from a document photo or scanned image, including receipts and photographed tables. Not for photo selection, metadata, or ordinary screen controls.
version: 1.0.0
interface_scope: generic
kind: generic
tags: [photo, transcription, image, fine-print]
source: authored
---

# Read text in a photo

## Hints

- Before transcribing small text or numbers from a fit-to-screen photo, obtain
  a detailed view even if the first reading seems plausible. If `observe_screen`
  offers `detail`, request the relevant region with its labels; omit the region
  when its location is unknown. Otherwise use the viewer's supported zoom.
- Read the value together with its row/field label. Reuse a detailed view already
  obtained; do not repeatedly zoom or recheck an established reading. Ordinary
  navigation and selecting photos by appearance need no detail view.
