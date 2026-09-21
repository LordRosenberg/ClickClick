"""Portable, non-executable encoding of the published synthetic task instances."""
import base64
import dataclasses
import io
import json
from pathlib import Path


def encode(value):
    from PIL import Image
    if isinstance(value, Image.Image):
        stream = io.BytesIO()
        value.save(stream, format="PNG")
        return {"$type": "image", "png": base64.b64encode(stream.getvalue()).decode("ascii")}
    if dataclasses.is_dataclass(value):
        name = type(value).__name__
        if name not in {"Expense", "Recipe", "CalendarEvent"}:
            raise ValueError("Unsupported fixture record type")
        return {"$type": name, "fields": {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, tuple):
        return {"$type": "tuple", "items": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        if any(not isinstance(k, str) or k == "$type" for k in value):
            raise ValueError("Unsupported fixture dictionary key")
        return {k: encode(v) for k, v in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("Unsupported fixture value: " + type(value).__name__)


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("$type")
    if kind is None:
        return {k: decode(v) for k, v in value.items()}
    if kind == "image":
        from PIL import Image
        with Image.open(io.BytesIO(base64.b64decode(value["png"], validate=True))) as image:
            return image.copy()
    if kind == "tuple":
        return tuple(decode(v) for v in value["items"])
    if kind in {"Expense", "Recipe", "CalendarEvent"}:
        from android_world.task_evals.utils import sqlite_schema_utils
        return getattr(sqlite_schema_utils, kind)(**decode(value["fields"]))
    raise ValueError("Unknown fixture type")


def load(path: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    if document["format"] != "clickclick-androidworld-fixtures-v1":
        raise ValueError("Unknown fixture format")
    records = decode(document["records"])
    required = {"task", "params", "seed", "max_steps", "max_model_calls", "max_seconds", "apps"}
    if len(records) != 116 or len({r["task"] for r in records}) != 116:
        raise ValueError("Expected 116 unique frozen task instances")
    if any(required - r.keys() for r in records):
        raise ValueError("Incomplete frozen task instance")
    return records
