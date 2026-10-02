---
name: mastodon-account-web-settings
description: "Mastodon: account language and advanced settings that are separate from native posting preferences."
version: 1.0.1
app: org.joinmastodon.android.mastodon
interface_scope: app
kind: workflow
capability: configure_mastodon_account
tags: [mastodon, settings, language, featured-tags, preferences]
source: authored
---

# Account settings

## Procedure

### Hints

- `Posting language` sets the language of new posts. To change the account's
  interface language, use the instance website's Preferences > Appearance.
- Featured hashtags and automatic post deletion are available in the instance's
  web settings. Use the instance address shown in the app, retaining `https`.
- In Automated post deletion, each checked exception independently keeps posts;
  selecting one does not clear the others. `Save changes` is at the page top.
- Native and browser sessions can belong to different accounts. Check the
  account shown in web settings before changing it; switching the native account
  does not switch the browser session.

## Verification

- Use the returned settings page's saved value or success confirmation for the
  selected account.
