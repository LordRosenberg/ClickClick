---
name: chrome-page-navigation
description: Chrome navigation, download handling, and compact browsing of web search results.
version: 1.0.3
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

- Bare queries in the address bar use the default search engine. Changing query
  words does not change that provider; if it is blocked, navigate explicitly to
  an alternative search engine rather than sending another bare query.
- The address bar can show a new URL while the previous document remains visible.
  With a visible loading indicator or mismatched destination/body, allow the pending
  navigation to settle and take a fresh observation before treating it as a failed
  lookup or replacing the destination. If still loading, a bounded `sleep` can
  allow more time; inspect the subsequent screen. A persistent error or stalled
  load can justify another route, not endless waiting.
- A direct image or document link may download instead of replacing the page.
  Use the download banner's Open/Details control or Downloads to open the completed
  intended file. A pending download is not proof the source is unreadable; inspect
  its completion or error before abandoning an already located artifact.
- For a particular title in a result list, use the site's title filter and compact
  or collapsed-summary view when available. Expanded abstracts can make a few
  results span many screens; finding the title does not require reading each abstract.
