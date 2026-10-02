---
name: chrome-page-navigation
description: Chrome loading states, verification pages, Google source links, downloads, and compact search results.
version: 1.0.11
app_aliases: [Chrome]
app: com.android.chrome
interface_scope: app
kind: app_core
role_sections: true
source: authored
---

# Chrome page navigation

## Execution

### Hints

- Chrome's thin progress bar below the address bar, a new URL with the old page,
  or Thinking/Generating/Loading or animated placeholders in the requested result
  indicates loading. Use `observe_screen` to check completion before changing
  the URL/query or acting on changing page controls. Continue when the
  relevant content is ready; unrelated ads/videos do not require waiting.
  A persistently stalled load or an explicit error allows recovery.
- On a CAPTCHA or Cloudflare page, click the visible verification checkbox or
  confirmation and follow the displayed instructions. While verification is
  running, use `observe_screen` to check its state; do not click or reload mid-check.
  If the checkbox resets, retry once after it settles. Continue when destination
  content appears; only persistent resets or errors justify another route.
- When checking information from Google AI Overview, open the citation/link icon
  beside that statement; it may be labelled `Related results`. For a search
  snippet, open that result.
- Bare queries in the address bar use the default search engine. Changing query
  words does not change that provider; if access remains blocked after handling
  the displayed verification or error, navigate explicitly to
  an alternative search engine rather than sending another bare query.
- A direct image or document link may download instead of replacing the page.
  Use the download banner's Open/Details control or Downloads to open the completed
  intended file. A pending download is not proof the source is unreadable; inspect
  its completion or error before abandoning an already located artifact.
- For a particular title in a result list, use the site's title filter and compact
  or collapsed-summary view when available. Expanded abstracts can make a few
  results span many screens; finding the title does not require reading each abstract.
