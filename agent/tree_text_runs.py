"""Lossless text-leaf arrays; eligibility comes from the original UI tree."""
from __future__ import annotations

import json
import re
from collections.abc import Sequence

TEXT = re.compile(r'depth=(\d+) \| TextView \| text=("(?:[^"\\]|\\.)*")')
ARRAY = re.compile(r'depth=(\d+) \| TextView \| texts=(\[.*\])')
# Original parent, sibling position, accessibility window and display.
Sibling = tuple[int, int, int | None, int | None]


def expand_text_runs(tree: str) -> str:
    """Restore each array item as one original accessibility text node."""
    lines: list[str] = []
    for line in tree.split("\n"):
        match = ARRAY.fullmatch(line)
        if match is None:
            lines.append(line)
            continue
        values = json.loads(match[2])
        if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            raise ValueError("Text run must contain only strings")
        lines.extend(f"depth={match[1]} | TextView | text=" + json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                     for v in values)
    return "\n".join(lines)


def encode_text_runs(rows: Sequence[tuple[str, Sibling | None]]) -> str:
    """Group consecutive eligible siblings, without inferring app semantics."""
    original = "\n".join(line for line, _ in rows)
    lines: list[str] = []
    index = 0
    while index < len(rows):
        line, sibling = rows[index]
        match = TEXT.fullmatch(line) if sibling is not None else None
        if match is None:
            lines.append(line)
            index += 1
            continue
        values, end = [match[2]], index + 1
        while end < len(rows):
            next_line, following = rows[end]
            next_match = TEXT.fullmatch(next_line)
            if (following is None or next_match is None or next_match[1] != match[1]
                    or following[0] != sibling[0] or following[2:] != sibling[2:]
                    or following[1] != sibling[1] + end - index):
                break
            values.append(next_match[2])
            end += 1
        packed = f"depth={match[1]} | TextView | texts=[" + ",".join(values) + "]"
        unpacked = "\n".join(row[0] for row in rows[index:end])
        if len(values) >= 3 and len(packed) < len(unpacked):
            lines.append(packed)
        else:
            lines.extend(row[0] for row in rows[index:end])
        index = end
    encoded = "\n".join(lines)
    # Any unrecognised representation falls back to the exact old grammar.
    try:
        return encoded if expand_text_runs(encoded) == original else original
    except (ValueError, TypeError):
        return original
