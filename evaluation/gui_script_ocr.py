"""Optional local OCR for explicit script identity checks."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import subprocess
from pathlib import Path

from PIL import Image

from agent.skills.script_visual import PixelBox


class LocalScriptOCR:
    """Recognize words and complete text lines with a local Tesseract executable."""

    def __init__(self, executable: Path, language: str = "eng") -> None:
        self.executable = executable.resolve(strict=True)
        if not self.executable.is_file() or not re.fullmatch(r"[A-Za-z0-9_+]+", language):
            raise ValueError("provide a local OCR executable and language identifiers")
        self.language = language
        self._frame_key = None
        self._observed: tuple[tuple[str, PixelBox], ...] = ()

    def __call__(self, image: Image.Image) -> tuple[tuple[str, PixelBox], ...]:
        key = (image.size, image.mode, hashlib.sha256(image.tobytes()).digest())
        if self._frame_key == key:
            return self._observed
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        try:
            output = subprocess.run(
                [str(self.executable), "stdin", "stdout", "-l", self.language, "tsv"],
                input=buffer.getvalue(), capture_output=True, check=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.decode("utf-8")
            fields = _text_boxes(output, image.size)
        except (OSError, subprocess.SubprocessError, TypeError, UnicodeError, ValueError) as exc:
            raise ValueError("visual_ocr_failed") from exc
        self._frame_key, self._observed = key, fields
        return fields


def _text_boxes(source: str, size: tuple[int, int]) -> tuple[tuple[str, PixelBox], ...]:
    lines: dict[tuple[int, ...], list[tuple[str, PixelBox]]] = {}
    reader = csv.DictReader(io.StringIO(source), delimiter="\t")
    required = {"level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width", "height", "text"}
    if not required.issubset(reader.fieldnames or ()):
        raise ValueError("unrecognized OCR table")
    for row in reader:
        word = (row["text"] or "").strip()
        if row["level"] != "5" or not word:
            continue
        x, y, width, height = (int(row[key]) for key in ("left", "top", "width", "height"))
        if not (0 <= x < x + width <= size[0] and 0 <= y < y + height <= size[1]):
            continue
        group = tuple(int(row[key]) for key in ("page_num", "block_num", "par_num", "line_num"))
        lines.setdefault(group, []).append((word, (x, y, x + width, y + height)))
    result = []
    for words in lines.values():
        result.extend(words)
        if len(words) > 1:
            bounds = [box for _, box in words]
            result.append((" ".join(word for word, _ in words),
                           (min(box[0] for box in bounds), min(box[1] for box in bounds),
                            max(box[2] for box in bounds), max(box[3] for box in bounds))))
    return tuple(result)
