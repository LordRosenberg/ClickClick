---
name: chrome-github-repository-stats
description: Read GitHub repository stars and contributor counts when the mobile layout hides repository statistics.
version: 1.0.0
app: com.android.chrome
interface_scope: app
kind: workflow
capability: github_repository_stats
tags: [github, repository, stars, contributors]
source: authored
---

# GitHub repository statistics

## Procedure

- If the mobile repository page omits the star count, enable Chrome menu >
  Desktop site, then return to the repository header and read the count beside
  Star. Page search cannot reveal a control hidden by the mobile layout.
- Read contributor totals from the repository's Contributors heading, rather
  than counting the preview avatars.

## Verification

- Use the labeled count for the intended repository. If it is abbreviated,
  retain that precision instead of inventing an exact count.
