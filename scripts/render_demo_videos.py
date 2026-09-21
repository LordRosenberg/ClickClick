"""Make README demos from successful episode recordings (PyAV + Pillow).

Example:
    python scripts/render_demo_videos.py --episodes PATH/episodes --output docs/assets/demos

The source recording is preserved in full at a constant playback speed. Preview
GIFs are sampled highlights and are explicitly labelled separately.
"""

import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import textwrap

import av
from PIL import Image, ImageDraw, ImageFont


DEMOS = [
    {
        "task": "RecipeAddMultipleRecipesFromMarkor",
        "slug": "notes-to-recipes",
        "title": "From notes\nto recipes",
        "apps": "Markor  >  Broccoli",
        "description": "Read three recipes from a note. Transfer their ingredients, directions and other fields into separate recipe records.",
        "highlights": ["Read the source note", "Fill multiple recipe forms", "Inspect the saved recipes"],
        "outcome": "3 recipes created",
    },
    {
        "task": "ExpenseAddMultipleFromGallery",
        "slug": "image-to-expenses",
        "title": "From an image\nto expense records",
        "apps": "Simple Gallery Pro  >  Pro Expense",
        "description": "Read expenses from an image, then enter each amount, category and note in the expense app.",
        "highlights": ["Read the source image", "Transfer three expense records", "Check the saved fields"],
        "outcome": "3 expenses created",
    },
    {
        "task": "SimpleCalendarAddRepeatingEvent",
        "slug": "recurring-calendar-event",
        "title": "A complete\nrecurring event",
        "apps": "Simple Calendar Pro",
        "description": "Create a 45-minute event with a specified date, start time, description and daily recurrence.",
        "highlights": ["Set the date and time", "Configure daily recurrence", "Inspect the saved event"],
        "outcome": "Daily event configured",
    },
    {
        "task": "ExpenseDeleteDuplicates2",
        "slug": "deduplicate-expenses",
        "title": "Find duplicates.\nKeep unique records.",
        "apps": "Pro Expense",
        "description": "Inspect a long expense list, identify exact duplicates and keep one copy of every unique expense.",
        "highlights": ["Inspect records across the list", "Compare duplicate candidates", "Preserve distinct expenses"],
        "outcome": "Exact duplicates removed",
    },
    {
        "task": "RetroCreatePlaylist",
        "slug": "ordered-playlist",
        "title": "The right songs.\nIn the right order.",
        "apps": "Retro Music",
        "description": "Create a named playlist, find two requested songs and add them in the specified order.",
        "highlights": ["Create the named playlist", "Find and add both songs", "Verify the final song order"],
        "outcome": "2 songs, requested order",
    },
    {
        "task": "SportsTrackerTotalDurationForCategoryThisWeek",
        "slug": "weekly-activity-summary",
        "title": "From activity logs\nto a weekly total",
        "apps": "OpenTracks",
        "description": "Find this week's swimming activities, inspect their durations and report the total in minutes.",
        "highlights": ["Identify the weekly date range", "Inspect swimming durations", "Report the total in minutes"],
        "outcome": "Answer: 510 minutes",
    },
]


def font(size, bold=False):
    system_fonts = Path(os.environ.get("WINDIR", "")) / "Fonts"
    names = ([str(system_fonts / "segoeuib.ttf"), "DejaVuSans-Bold.ttf"] if bold
             else [str(system_fonts / "segoeui.ttf"), "DejaVuSans.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def compose(raw, demo, result, source_seconds, duration, crop, speed, preview=False):
    canvas = Image.new("RGB", (1280, 1000), "#101820")
    d = ImageDraw.Draw(canvas)
    d.text((56, 58), "CLICKCLICK / WORKFLOW DEMO", font=font(23, True), fill="#70ddbd")
    d.multiline_text((56, 136), demo["title"], font=font(49, True), fill="#f6f8fa", spacing=10)
    d.text((56, 295), demo["apps"], font=font(26), fill="#70ddbd")
    y = 367
    for line in textwrap.wrap(demo["description"], width=42):
        d.text((56, y), line, font=font(25), fill="#c1cbd4")
        y += 38
    y += 48
    for i, line in enumerate(demo["highlights"], 1):
        d.text((56, y), f"0{i}", font=font(23, True), fill="#70ddbd")
        d.text((105, y), line, font=font(25), fill="#f6f8fa")
        y += 49
    d.line((56, 792, 665, 792), fill="#34414c", width=2)
    d.text((56, 814), demo["outcome"], font=font(29, True), fill="#f6f8fa")
    d.text((56, 859), f"{result['episode_steps']} actions  /  Task passed", font=font(22), fill="#c1cbd4")
    label = "PREVIEW / OPEN FULL VIDEO" if preview else f"{speed:g}x PLAYBACK / FULL RECORDING"
    d.text((56, 913), label, font=font(20, True), fill="#70ddbd")
    d.text((56, 951), f"Source recording  {clock(source_seconds)} / {clock(duration)}", font=font(18), fill="#9baab7")
    phone = raw.crop(crop)
    phone.thumbnail((464, 980), Image.Resampling.LANCZOS)
    canvas.paste(phone, (754 + (464-phone.width)//2, (1000-phone.height)//2))
    return canvas


def render(demo, args):
    episode = args.episodes / demo["task"] / "plan_executor"
    source = episode / "host-video.mp4"
    result = json.loads((episode / "result.json").read_text(encoding="utf-8"))
    if not (result.get("valid") and result.get("raw_official_score") == 1
            and result.get("status") == "succeeded" and result.get("budgeted_success")):
        raise ValueError(f"Not a verified successful run: {demo['task']}")
    output = args.output / f"{demo['slug']}.mp4"
    with av.open(str(source)) as src:
        stream = src.streams.video[0]
        duration = float(stream.duration * stream.time_base)
        with av.open(str(output), "w", options={"movflags": "+faststart"}) as dst:
            encoded = dst.add_stream("libx264", rate=15)
            encoded.width, encoded.height, encoded.pix_fmt = 1280, 1000, "yuv420p"
            encoded.options = {"crf": "23", "preset": "fast"}
            next_sample, index = 0.0, 0
            for frame in src.decode(video=0):
                seconds = float(frame.time or 0)
                if seconds + 1e-5 < next_sample:
                    continue
                picture = compose(frame.to_image(), demo, result, seconds, duration, args.crop, args.speed)
                video_frame = av.VideoFrame.from_image(picture)
                video_frame.pts, video_frame.time_base = index, Fraction(1, 15)
                for packet in encoded.encode(video_frame):
                    dst.mux(packet)
                index += 1
                next_sample = index * args.speed / 15
            for packet in encoded.encode():
                dst.mux(packet)
    previews = []
    with av.open(str(source)) as src:
        for fraction in [0.10, 0.20, 0.35, 0.45, 0.60, 0.72, 0.85, 0.985]:
            src.seek(int(duration * fraction * av.time_base))
            frame = next(src.decode(video=0))
            picture = compose(frame.to_image(), demo, result, float(frame.time), duration,
                              args.crop, args.speed, preview=True)
            picture.thumbnail((640, 500), Image.Resampling.LANCZOS)
            previews.append(picture)
    previews[0].save(args.output / f"{demo['slug']}.gif", save_all=True,
                     append_images=previews[1:], duration=1400, loop=0, optimize=True)
    previews[-1].save(args.output / f"{demo['slug']}.png")
    meta = {
        "task": demo["task"], "task_id": result["task_id"], "video": output.name,
        "source_recording_seconds": round(duration, 3), "playback_speed": args.speed,
        "output_seconds": round(index / 15, 3), "task_seconds": result["elapsed_s"],
        "actions": result["episode_steps"], "official_score": result["raw_official_score"],
        "crop_xyxy": args.crop, "preview": "eight sampled frames, labelled preview",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    print(json.dumps(meta), flush=True)
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--speed", type=float, default=3)
    parser.add_argument("--crop", type=lambda s: tuple(map(int, s.split(","))), default=(0, 0, 464, 980))
    args = parser.parse_args()
    if args.speed <= 0 or len(args.crop) != 4:
        parser.error("speed must be positive; crop requires x0,y0,x1,y1")
    args.output.mkdir(parents=True, exist_ok=True)
    results = [render(demo, args) for demo in DEMOS]
    (args.output / "manifest.json").write_text(json.dumps({
        "source_batch": args.episodes.parent.name, "demos": results,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
