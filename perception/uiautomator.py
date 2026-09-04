"""Parse `uiautomator dump` XML into the nested dict shape consumed by normalizer.

uiautomator XML nodes carry attributes like:
  class, text, content-desc, bounds="[x1,y1][x2,y2]", clickable, ...
We map them to the normalizer's expected keys:
  class, text, desc, bounds=[x1,y1,x2,y2], clickable, children.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

# Raw uiautomator XML boolean attrs we preserve on the nested-dict node shape.
# Hyphenated XML names are normalized once here so downstream callers can read
# stable snake_case keys without caring about XML spelling.
_BOOL_ATTRS: dict[str, str] = {
    "clickable": "clickable",
    "checkable": "checkable",
    "enabled": "enabled",
    "focused": "focused",
    "checked": "checked",
    "selected": "selected",
    "focusable": "focusable",
    "scrollable": "scrollable",
    "password": "password",
    "editable": "editable",
    "long-clickable": "long_clickable",
}


def _parse_bounds(raw: str) -> list[int]:
    """Parse a uiautomator bounds string `[x1,y1][x2,y2]` into `[x1,y1,x2,y2]`."""
    if not raw:
        return [0, 0, 0, 0]
    m = _BOUNDS_RE.search(raw)
    if not m:
        return [0, 0, 0, 0]
    return [int(g) for g in m.groups()]


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() == "true"


def _node_to_dict(node: ET.Element) -> dict[str, Any]:
    """Convert one XML element to the normalizer's nested-dict shape."""
    attrs = node.attrib
    result: dict[str, Any] = {
        "class": attrs.get("class", ""),
        "text": attrs.get("text", ""),
        "desc": attrs.get("content-desc", "") or attrs.get("contentDescription", ""),
        "hint": (
            attrs.get("hint", "")
            or attrs.get("hint-text", "")
            or attrs.get("hintText", "")
            or attrs.get("placeholder", "")
        ),
        "bounds": _parse_bounds(attrs.get("bounds", "")),
        # Historical contract: missing `clickable` / `enabled` still surface with
        # defaults so downstream callers do not special-case absent keys.
        "clickable": _bool(attrs.get("clickable")),
        "enabled": _bool(attrs.get("enabled"), default=True),
        "package": attrs.get("package", ""),
        "resource_id": attrs.get("resource-id", ""),
    }
    for raw_name, normalized_name in _BOOL_ATTRS.items():
        if raw_name in ("clickable", "enabled"):
            continue
        if raw_name in attrs:
            result[normalized_name] = _bool(attrs.get(raw_name))
    children: list[dict[str, Any]] = []
    for child in node:
        if isinstance(child.tag, str):
            children.append(_node_to_dict(child))
    if children:
        result["children"] = children
    return result


def parse_uiautomator_xml(xml_text: str) -> dict[str, Any]:
    """Parse uiautomator dump XML into a nested dict tree.

    Returns the root node dict (the `<hierarchy>` wrapper is unwrapped to its
    single child when present, matching the normalizer's expected root shape).
    """
    if not xml_text or not xml_text.strip():
        return {"class": "", "children": []}
    root = ET.fromstring(xml_text)
    # uiautomator wraps the real tree in <hierarchy><node .../>...</hierarchy>.
    if root.tag == "hierarchy":
        child_nodes = [c for c in root if isinstance(c.tag, str)]
        if len(child_nodes) == 1:
            return _node_to_dict(child_nodes[0])
        # Multiple or zero children: synthesize a wrapper.
        return {"class": "hierarchy", "children": [_node_to_dict(c) for c in child_nodes]}
    return _node_to_dict(root)


def extract_package(xml_text: str) -> str:
    """Best-effort extraction of the foreground package from uiautomator XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    for elem in root.iter():
        pkg = elem.attrib.get("package")
        if pkg:
            return pkg
    return ""
