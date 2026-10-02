---
name: mastodon-navigation
description: Mastodon account switching and search entry activation.
version: 1.0.2
app_aliases: [Mastodon]
app: org.joinmastodon.android.mastodon
interface_scope: app
kind: app_core
role_sections: true
source: authored
---

# Mastodon navigation

## Execution

### Hints

- The Android share flow may list several Mastodon accounts. Its first row is
  not evidence of the app's active account. Choose the requested account; when
  none is specified, use the already active account, checking the app's profile
  once if its identity is not yet known.

- When switching native accounts, long-press the Profile tab to open the account
  list, then choose the intended account.
- When the Explore page shows `Search Mastodon` as a clickable TextView, tap it
  to open the editor before entering the query. It is a search entry point, not
  an editable field; if an EditText is already open, enter text directly.
