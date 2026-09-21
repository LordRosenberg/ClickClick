"""Lossless text reuse within one request; original records stay untouched."""

import copy
import hashlib
import json
import math
from collections import Counter

from agent.tool_registry import count_message_images


def estimate_context(messages: list[dict]) -> int:
    def without_images(value):
        if isinstance(value, dict):
            return {key: without_images(item) for key, item in value.items() if key != "image_url"}
        if isinstance(value, list):
            return [without_images(item) for item in value]
        return value

    return math.ceil(len(json.dumps(without_images(messages), ensure_ascii=False)) / 3) + 1200 * count_message_images(messages)


def _observation_text_blocks(messages):
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        if not any(block.get("type") == "image_url" for block in content if isinstance(block, dict)):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and len(block.get("text", "")) >= 1200:
                yield block


def reuse_observation_text(messages: list[dict]) -> list[dict]:
    """Reuse identical long historical observation text, keeping every image.

    Only multimodal user observation blocks qualify. No assistant claims, tool
    results, notes or current observation are removed. Reuse references resolve
    within the returned list, including after restoration/compaction.
    """
    counts = Counter(block["text"] for block in _observation_text_blocks(messages))
    if not any(count > 1 for count in counts.values()):
        return messages
    result = copy.deepcopy(messages)
    seen: dict[str, str] = {}
    for block in _observation_text_blocks(result):
        text = block["text"]
        if counts[text] < 2:
            continue
        if text in seen:
            block["text"] = f"Observation text repeats {seen[text]} exactly. This occurrence and its image remain separate evidence."
        else:
            ref = "observation-text-" + hashlib.sha256(text.encode()).hexdigest()[:12]
            seen[text] = ref
            block["text"] = f"[{ref}]\n{text}"
    return result
