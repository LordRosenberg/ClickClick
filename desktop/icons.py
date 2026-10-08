"""Encode the bundled application artwork for native desktop entries."""

from pathlib import Path

from PIL import Image


APP_ICON = Path(__file__).parent / "assets/clickclick-app.png"


def write_app_icon(target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(APP_ICON) as source:
        artwork = source.convert("RGBA")
        if target.suffix.lower() == ".ico":
            artwork.save(target, format="ICO", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
        elif target.suffix.lower() == ".icns":
            artwork.save(target, format="ICNS")
        else:
            raise ValueError("Application icon must be ICO or ICNS")
    return target
