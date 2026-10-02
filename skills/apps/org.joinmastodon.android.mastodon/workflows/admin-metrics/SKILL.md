---
name: mastodon-admin-metrics
description: "Mastodon administrator backend: read database size or unresolved reports, including browser login and hidden navigation."
version: 1.0.0
app: org.joinmastodon.android.mastodon
interface_scope: app
kind: workflow
capability: read_mastodon_admin_metrics
tags: [mastodon, administrator, backend, database, reports]
source: authored
---

# Read administrator metrics

## Procedure

- In native account settings, `Account settings` is a section heading and
  `Privacy and reach` opens native preferences. For backend metrics, open the
  displayed instance address in Chrome with HTTPS. Browser login is independent:
  to change it, open web Preferences, expand `Toggle menu`, use `Logout`, and
  sign in with the supplied account credentials.
- In the web settings/admin sidebar, expand `Toggle menu` and scroll the menu
  to reveal lower entries. Database size is under `PgHero` > `Space`.
- For outstanding reports, use `Reports` with status `Unresolved`, or the
  dashboard's explicit `pending reports` count. `Reports opened` and `Reports
  resolved` describe activity within a date range, not the outstanding total.

## Verification

- Read the requested metric and unit from the authenticated backend. Keep
  the browser's account separate from the native app's account when delivering it.
