---
name: mastodon-share-to-account
description: "Mastodon: share content from another app using the intended signed-in account."
version: 1.0.1
app: org.joinmastodon.android.mastodon
interface_scope: app
kind: workflow
capability: share_content_to_mastodon
tags: [mastodon, sharing, accounts, attachments]
source: authored
---

# Share to an account

## Procedure

### Hints

- The share composer can use a different account from the native app's current
  timeline. Match the displayed composer identity before posting; returning to
  the app does not establish which account received the shared post.

## Verification

- Use the composer's identity and attachment preview before posting, and the
  posting receipt. Do not switch accounts merely to search for a second receipt.
