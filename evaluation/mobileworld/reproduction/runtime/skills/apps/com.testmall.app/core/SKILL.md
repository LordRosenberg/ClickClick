---
name: taodian-layered-pages
description: Distinguish active pages and product-option overlays from covered controls in Taodian.
version: 1.0.3
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

- For an existing cart, retain the prefilled login account unless an account
  change is requested. A shipping recipient phone belongs to the address form.
- After filling the SMS code, select “已阅读” before submitting
  “同意协议并登录”; that button does not select the checkbox.
- Immediately after login, the cart can briefly show zero items while loading.
  Wait for the returned cart to settle before concluding it is empty; this
  transition does not justify repeatedly rechecking an established empty cart.
- Search and product pages can cover earlier pages while both remain in the
  tree. An index drawn over a visible button may belong to the covered page.
  Use it only when its tree label also matches the visible control; otherwise
  tap the visible control by coordinates.
- Product-option sheets can show a purchase button absent from the tree, while
  the underlying product page exposes another button with the same label. Tap
  the visible sheet button by coordinates when no matching sheet node exists.
