"""Sample published desktop installer asset downloads and render a README chart.

GitHub counts downloads, not people. Historical daily totals are only available
from the first sample; a deleted asset retains its last observed count.
"""

import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

INSTALLER = re.compile(r"^ClickClick-[0-9][0-9A-Za-z.+-]*-(?:windows-x86_64\.exe|macos-(?:x86_64|aarch64)\.zip)$")


def sample(history, releases, date, *, sampled_at=None):
    result = json.loads(json.dumps(history))
    assets = result.setdefault("assets", {})
    for release in releases:
        if release.get("draft"):
            continue
        for asset in release.get("assets", []):
            if INSTALLER.fullmatch(asset["name"]):
                identity = str(asset["id"])
                assets[identity] = {"name": asset["name"], "downloads": max(
                    asset["download_count"], assets.get(identity, {}).get("downloads", 0))}
    total = sum(a["downloads"] for a in assets.values())
    points = {point["date"]: point["downloads"] for point in result.get("daily", [])}
    points[date] = total
    result.update(schema=1, daily=[{"date": day, "downloads": value} for day, value in sorted(points.items())])
    if sampled_at is not None:
        result["sampled_at"] = sampled_at
    return result


def chart(history):
    points = history["daily"]
    latest = points[-1]
    high = max(1, max(p["downloads"] for p in points))
    coordinates = [(72 + i * 658 / max(1, len(points) - 1), 230 - 150 * p["downloads"] / high) for i, p in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#5b69e8"/>' for x, y in coordinates)
    start = html.escape(points[0]["date"])
    end = html.escape(latest["date"])
    updated = html.escape(history.get("sampled_at", end)).replace("T", " ").replace("Z", " UTC")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="800" height="320" viewBox="0 0 800 320" role="img" aria-label="Installer downloads: {latest['downloads']}">
<rect width="800" height="320" rx="12" fill="#fff"/>
<g font-family="Arial,sans-serif" fill="#27334b">
<text x="32" y="36" font-size="20" font-weight="bold">ClickClick installer downloads</text>
<text x="730" y="36" font-size="22" text-anchor="end" font-weight="bold">{latest['downloads']:,}</text>
<path d="M72 70V230H730" fill="none" stroke="#bbc4d3"/>
<text x="60" y="86" text-anchor="end" font-size="12">{high:,}</text><text x="60" y="234" text-anchor="end" font-size="12">0</text>
<polyline points="{line}" fill="none" stroke="#5b69e8" stroke-width="3"/>{dots}
<text x="72" y="253" font-size="12">{start}</text><text x="730" y="253" text-anchor="end" font-size="12">{end}</text>
<text x="32" y="283" font-size="12">Updated {updated} · hourly refresh · daily totals</text>
<text x="32" y="303" font-size="12">Includes repeat downloads and upgrades · not unique users · history starts {start}</text>
</g></svg>\n'''


def fetch_releases(repository):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ClickClick-project-stats", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    releases = []
    page = 1
    while True:
        with urlopen(Request(f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}", headers=headers), timeout=30) as response:
            batch = json.load(response)
        releases.extend(batch)
        if len(batch) < 100:
            return releases
        page += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="LordRosenberg/ClickClick")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Invalid repository")
    args.output.mkdir(parents=True, exist_ok=True)
    file = args.output / "installer-downloads.json"
    history = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
    releases = fetch_releases(args.repository)
    now = datetime.now(timezone.utc)
    history = sample(history, releases, now.date().isoformat(),
                     sampled_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"))
    file.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    (args.output / "installer-downloads.svg").write_text(chart(history), encoding="utf-8")
    print(f"Published installer downloads: {history['daily'][-1]['downloads']}")


if __name__ == "__main__":
    main()
