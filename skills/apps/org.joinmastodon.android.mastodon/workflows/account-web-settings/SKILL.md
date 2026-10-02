---
name: mastodon-account-web-settings
description: "Mastodon web: invite links; interface language; featured hashtags; automatic post deletion."
version: 1.0.5
app: org.joinmastodon.android.mastodon
interface_scope: app
kind: workflow
capability: configure_mastodon_account
tags: [mastodon, settings, invites, language, featured-tags, preferences]
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
- Browser and native sessions are independent; switching the native account
  does not switch the browser account.

#### When creating invite links

- For invitation creation, open the instance's HTTPS origin with path `/invites`
  in the browser. Use the instance address already shown by the app or its
  website; preserve any port. Invite management is on the website, not in the
  native settings list.
- `Invite to follow your account` refers to the account generating the invite
  in the browser.

## Verification

- For preference changes, use the returned settings page's saved value or
  success confirmation for the selected account.
