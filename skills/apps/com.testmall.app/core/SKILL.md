---
name: taodian-layered-pages
description: Distinguish active pages and product-option overlays from covered controls in Taodian.
version: 1.0.1
app_aliases: [Taodian, 淘店]
app: com.testmall.app
interface_scope: app
kind: app_core
role_sections: true
source: authored
---

# Taodian layered pages

## Execution

### Hints

- On the SMS login page, first select the checkbox beside “已阅读”, then tap
  “同意协议并登录”. The login button does not select the checkbox for you.
- Search and product pages can cover earlier pages while both remain in the
  tree. Match controls to the screenshot and the current page's WebView branch;
  covered home/search-result controls can still have clickable indices.
- Product-option sheets can show a purchase button absent from the tree, while
  the underlying product page exposes another button with the same label. Tap
  the visible sheet button by coordinates when no matching sheet node exists.
