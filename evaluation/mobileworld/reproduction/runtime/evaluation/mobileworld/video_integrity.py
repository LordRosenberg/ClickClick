"""Decode retained videos through EOF; sparse frames alone do not imply loss."""
from __future__ import annotations

from pathlib import Path
import json
import os
import struct
import subprocess
import sys
from typing import Any


class VideoIntegrityError(RuntimeError):
    """A recorded file cannot be decoded as an intact video stream."""


def _check_mp4_boxes(path: Path) -> None:
    """Reject truncated containers before handing their contents to native code."""
    size = path.stat().st_size
    types = set()
    with path.open('rb') as stream:
        offset = 0
        while offset < size:
            header = stream.read(8)
            if len(header) != 8:
                raise VideoIntegrityError('truncated MP4 box header')
            length, kind = struct.unpack('>I4s', header)
            header_size = 8
            if length == 1:
                extra = stream.read(8)
                if len(extra) != 8:
                    raise VideoIntegrityError('truncated extended MP4 box header')
                length = struct.unpack('>Q', extra)[0]
                header_size = 16
            elif length == 0:
                length = size-offset
            if length < header_size or offset+length > size:
                raise VideoIntegrityError('invalid or truncated MP4 box')
            types.add(kind)
            offset += length
            stream.seek(offset)
    if not {b'ftyp', b'moov', b'mdat'} <= types:
        raise VideoIntegrityError('MP4 is missing required container boxes')


def inspect_video(path: Path, *, timeout: float = 90) -> dict[str, Any]:
    """Isolate native decoder faults from the evaluator and official scoring."""
    path = path.resolve()
    _check_mp4_boxes(path)
    try:
        result = subprocess.run(
            [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), str(path)],
            capture_output=True, text=True, encoding='utf8', errors='replace',
            timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoIntegrityError('recording decoder exceeded its time limit') from exc
    if result.returncode:
        raise VideoIntegrityError(f'recording decoder failed (exit {result.returncode})')
    try:
        report = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise VideoIntegrityError('recording decoder returned invalid diagnostics') from exc
    if report.get('validation') != 'decoded_to_eof' or report.get('decoded_frames', 0) <= 0:
        raise VideoIntegrityError('recording decoder did not validate the stream')
    return report


def _inspect_video(path: Path) -> dict[str, Any]:
    """Validate the entire stream and report timestamps without inventing frames.

    Android screenrecord can emit sparse frames while the screen is static.
    Neither a long frame interval nor a short static tail proves missing GUI
    activity. Callers must separately retain recorder and task lifecycle times.
    """
    import av

    count = 0
    first = last = None
    max_gap = 0.0
    dimensions: list[int] = []
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                raise VideoIntegrityError("recording has no video stream")
            stream = container.streams.video[0]
            # This runs at an episode boundary and should not saturate the host.
            stream.thread_count = 1
            for frame in container.decode(stream):
                if frame.is_corrupt:
                    raise VideoIntegrityError("recording contains a corrupt frame")
                value = frame.time
                if value is None:
                    raise VideoIntegrityError("recording frame has no presentation timestamp")
                if last is not None:
                    if value < last:
                        raise VideoIntegrityError("recording timestamps move backwards")
                    max_gap = max(max_gap, value-last)
                if first is None:
                    first = value
                    dimensions = [frame.width, frame.height]
                last = value
                count += 1
            duration = float(stream.duration * stream.time_base) if stream.duration is not None else None
    except VideoIntegrityError:
        raise
    except Exception as exc:
        raise VideoIntegrityError(f"recording decode failed: {type(exc).__name__}") from exc
    if count == 0:
        raise VideoIntegrityError("recording has no decoded frames")
    return {
        "validation": "decoded_to_eof", "decoded_frames": count,
        "first_frame_pts_s": first, "last_frame_pts_s": last,
        "duration_s": duration, "dimensions": dimensions,
        "max_frame_interval_s": max_gap,
    }


if __name__ == '__main__':
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    try:
        print(json.dumps(_inspect_video(Path(sys.argv[1]))))
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        sys.exit(1)
