---
name: mastodon-navigation
description: Mastodon account switching and search entry activation.
version: 1.0.6
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

- When sharing to Mastodon, select the requested account, or the native active
  account if none is specified. If no account is specified and the native
  identity is unknown, dismiss the chooser, launch Mastodon, read the Profile
  account handle, then return to the source app and share again. Chooser order
  does not identify the active account.

- When switching native accounts, long-press the Profile tab to open the account
  list, then choose the intended account.
- For invite-link creation, use the instance website's `/invites` page in the
  browser, retaining the instance's HTTPS origin. In the native account settings
  list, `Account settings` labels a section rather than opening web settings.
- When the Explore page shows `Search Mastodon` as a clickable TextView, tap it
  to open the editor before entering the query. It is a search entry point, not
  an editable field; if an EditText is already open, enter text directly.
