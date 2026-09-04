"""Minimal ADB helpers used only inside the Driver process.

Actions and screenshots remain ADB-direct. Semantic observation prefers the
small ClickClick accessibility collector and falls back to `uiautomator dump`.

Sync kernels (`uiautomator_dump`, `screencap`, `input_*`, ...) perform the
actual blocking `subprocess.run` calls and remain sync so tests can invoke
them directly. The `*_async` wrappers offload those kernels to a thread
executor via `asyncio.to_thread` so a Driver running inside an async event
loop (in-process with the Agent, or as its own process) never blocks the
loop on adb. Every kernel enforces a `timeout` (default 30s); the timeout
is honored under `to_thread` because the underlying `subprocess.run`
timeout fires on the worker thread and surfaces as `AdbError`.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

# uiautomator dump writes to a path on the device, then we pull it.
# A fixed device path avoids per-call temp names on the device side.
_DEVICE_DUMP_PATH = "/sdcard/clickclick_window_dump.xml"

# --- fix-stale-uiautomator-dump: defensive retry knobs --------------------
# Android's `uiautomator dump` returns exit 0 but writes a 0-byte file when
# the foreground window has a null accessibility root (immersive video,
# transitioning activities, fullscreen ads). The previous frame's XML still
# sits at the fixed device-side path and would be pulled as "fresh" data.
# We force a clean slate (rm) before each dump, verify the dump actually
# wrote a non-trivial file (wc -c size check), and retry on transient
# null-root failures.
_DUMP_MIN_BYTES: int = 100         # null-root failure mode produces 0-byte files
_DUMP_MAX_ATTEMPTS: int = 3        # transient null-root failures during transitions
_DUMP_WC_TIMEOUT: float = 5.0      # fast probe; the real dump timeout is 30s
_DUMP_RM_TIMEOUT: float = 5.0      # rm is non-fatal if it fails
_DUMP_BACKOFF_S: float = 0.2       # sleep between standalone multi-attempt retries


class AdbError(RuntimeError):
    """Raised when ADB is missing or a device interaction fails."""


class AdbTimeoutError(AdbError):
    """Raised when a cancellable ADB subprocess reaches its hard timeout."""


def _serial_args(serial: str | None) -> list[str]:
    return ["-s", serial] if serial else []


def shell(serial: str | None, args: list[str], *, timeout: float = 30.0) -> bytes:
    """Run one argument-safe `adb shell` command."""
    return _run([adb_bin(), *_serial_args(serial), "shell", *args], timeout=timeout)


async def shell_async(
    serial: str | None, args: list[str], *, timeout: float = 30.0
) -> bytes:
    """Async `adb shell` without blocking or leaving timed-out children."""
    return await _run_async(
        [adb_bin(), *_serial_args(serial), "shell", *args], timeout=timeout
    )


async def display_size_async(
    serial: str | None, *, timeout: float = 2.0
) -> tuple[int, int]:
    """Return the current Android logical display size from ``wm size``."""
    output = await shell_async(serial, ["wm", "size"], timeout=timeout)
    physical: tuple[int, int] | None = None
    override: tuple[int, int] | None = None
    for line in output.decode("utf-8", "replace").splitlines():
        label, _, value = line.partition(":")
        parts = value.strip().lower().split("x", 1)
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            continue
        size = (int(parts[0]), int(parts[1]))
        if size[0] <= 0 or size[1] <= 0:
            continue
        if "override" in label.lower():
            override = size
        elif "physical" in label.lower():
            physical = size
    size = override or physical
    if size is None:
        raise AdbError("wm size returned no usable display dimensions")
    return size


async def content_query_async(
    serial: str | None, uri: str, *, timeout: float = 1.0
) -> bytes:
    return await shell_async(
        serial, ["content", "query", "--uri", uri], timeout=timeout
    )


async def forward_localabstract_async(
    serial: str, socket_name: str, *, timeout: float = 5.0
) -> int:
    """Create a serial-scoped ADB forward and return its allocated TCP port."""
    if not serial or not socket_name or any(ch.isspace() for ch in socket_name):
        raise AdbError("invalid ADB forward target")
    output = await _run_async(
        [
            adb_bin(), "-s", serial, "forward", "tcp:0",
            f"localabstract:{socket_name}",
        ],
        timeout=timeout,
    )
    try:
        port = int(output.decode("ascii", "strict").strip())
    except (UnicodeError, ValueError) as exc:
        raise AdbError(f"ADB forward returned invalid port: {output!r}") from exc
    if not 0 < port <= 65535:
        raise AdbError(f"ADB forward returned out-of-range port: {port}")
    return port


async def remove_forward_async(
    serial: str, port: int, *, timeout: float = 5.0
) -> None:
    """Best-effort removal is handled by callers; this helper reports errors."""
    if not serial or not 0 < int(port) <= 65535:
        raise AdbError("invalid ADB forward removal target")
    await _run_async(
        [adb_bin(), "-s", serial, "forward", "--remove", f"tcp:{int(port)}"],
        timeout=timeout,
    )


async def list_forwards_to_localabstract_async(
    serial: str, socket_name: str, *, timeout: float = 2.0
) -> list[int]:
    """List TCP ports targeting one exact serial/socket pair."""
    if not serial or not socket_name or any(ch.isspace() for ch in socket_name):
        raise AdbError("invalid ADB forward target")
    output = await _run_async(
        [adb_bin(), "-s", serial, "forward", "--list"], timeout=timeout,
    )
    target = f"localabstract:{socket_name}"
    ports: list[int] = []
    for line in output.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] != serial or fields[2] != target:
            continue
        local = fields[1]
        if not local.startswith("tcp:") or not local[4:].isdigit():
            continue
        port = int(local[4:])
        if 0 < port <= 65535:
            ports.append(port)
    return ports


async def remove_forwards_to_localabstract_async(
    serial: str,
    socket_name: str,
    *,
    preserve_ports: set[int] | None = None,
    timeout: float = 2.0,
) -> list[int]:
    """Remove only exact-target forwards not present in a frozen baseline."""
    ports = await list_forwards_to_localabstract_async(
        serial, socket_name, timeout=timeout,
    )
    preserved = {int(port) for port in (preserve_ports or set())}
    removed = [port for port in ports if port not in preserved]
    await asyncio.gather(
        *(remove_forward_async(serial, port, timeout=timeout) for port in removed),
        return_exceptions=True,
    )
    return removed


async def push_file_async(
    serial: str, local_path: str | Path, remote_path: str, *, timeout: float = 10.0
) -> None:
    """Push one file through a cancellable, reaped ADB subprocess."""
    path = Path(local_path)
    if not serial or not path.is_file() or not remote_path.strip():
        raise AdbError("invalid ADB push arguments")
    await _run_async(
        [adb_bin(), "-s", serial, "push", str(path), remote_path],
        timeout=timeout,
    )


async def install_apk_async(
    serial: str | None, apk_path: str | Path, *, timeout: float = 90.0
) -> str:
    path = Path(apk_path)
    if not path.is_file():
        raise AdbError(f"APK not found: {path}")
    output = await _run_async(
        [adb_bin(), *_serial_args(serial), "install", "-r", str(path)], timeout=timeout
    )
    return output.decode("utf-8", "replace").strip()


async def package_version_async(serial: str | None, package: str) -> str | None:
    try:
        output = await shell_async(
            serial, ["dumpsys", "package", package], timeout=8.0
        )
    except AdbError:
        return None
    text = output.decode("utf-8", "replace")
    if "Unable to find package" in text or not text.strip():
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("versionName="):
            return line.split("=", 1)[1].strip()
    return "installed"


async def setting_get_async(
    serial: str | None, namespace: str, key: str, *, timeout: float = 5.0
) -> str:
    output = await shell_async(
        serial, ["settings", "get", namespace, key], timeout=timeout
    )
    value = output.decode("utf-8", "replace").strip()
    return "" if value == "null" else value


async def setting_put_async(
    serial: str | None,
    namespace: str,
    key: str,
    value: str,
    *,
    timeout: float = 5.0,
) -> None:
    await shell_async(
        serial, ["settings", "put", namespace, key, value], timeout=timeout
    )


async def ime_enable_async(serial: str | None, ime_id: str) -> None:
    await shell_async(serial, ["ime", "enable", ime_id], timeout=10.0)


async def default_ime_async(serial: str | None) -> str:
    return await setting_get_async(serial, "secure", "default_input_method")


async def appop_mode_async(
    serial: str | None, package: str, operation: str
) -> str:
    """Return one bounded AppOp mode, or empty when the OEM omits it."""
    output = await shell_async(
        serial, ["appops", "get", package, operation], timeout=5.0
    )
    text = output.decode("utf-8", "replace")
    marker = f"{operation}:"
    for line in text.splitlines():
        if marker not in line:
            continue
        value = line.split(marker, 1)[1].strip().split(";", 1)[0].strip()
        return value.split(None, 1)[0] if value else ""
    return ""


async def stay_awake_while_plugged_async(serial: str | None, enabled: bool) -> None:
    await shell_async(
        serial, ["svc", "power", "stayon", "true" if enabled else "false"],
        timeout=5.0,
    )


def adb_bin() -> str:
    """Locate the adb executable."""
    import shutil

    path = shutil.which("adb")
    if not path:
        raise AdbError("adb not found on PATH")
    return path


def _run(args: list[str], *, capture_bytes: bool = False, timeout: float = 30.0) -> bytes:
    """Run an adb command, returning stdout bytes. Raises AdbError on failure."""
    try:
        proc = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AdbError(f"adb invocation failed: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise AdbError(
            f"adb command failed ({exc.returncode}): "
            f"{(exc.stderr or b'').decode('utf-8', 'replace').strip()}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbTimeoutError(
            f"adb command timed out: {' '.join(args)}"
        ) from exc
    return proc.stdout if capture_bytes else proc.stdout  # type: ignore[return-value]


async def _run_async(args: list[str], *, timeout: float) -> bytes:
    """Run and reap one adb subprocess with cooperative asyncio cancellation."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=max(0.001, float(timeout))
            )
        except asyncio.TimeoutError as exc:
            await _terminate_and_reap(proc)
            raise AdbTimeoutError(
                f"adb command timed out: {' '.join(args)}"
            ) from exc
        if proc.returncode != 0:
            raise AdbError(
                f"adb command failed ({proc.returncode}): "
                f"{(stderr or b'').decode('utf-8', 'replace').strip()}"
            )
        return stdout or b""
    except FileNotFoundError as exc:
        raise AdbError(f"adb invocation failed: {exc}") from exc
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            await _terminate_and_reap(proc)
        raise


async def _terminate_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Reliably terminate, then kill and reap a subprocess if necessary."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=0.5)
        return
    except asyncio.TimeoutError:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


def list_device_serials() -> list[str]:
    """Return serials of connected devices in 'device' state."""
    out = subprocess.check_output([adb_bin(), "devices"], text=True)
    serials: list[str] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def require_single_device() -> str:
    """Return the sole connected device serial or raise.

    Prefer ``sole_online_device`` / explicit serials for multi-device labs.
    Kept for CLI one-shots that still expect exactly one phone.
    """
    return sole_online_device()


def sole_online_device() -> str:
    """Return the only online device serial, or raise if zero / more than one."""
    serials = list_device_serials()
    if not serials:
        raise AdbError("no authorized Android device connected")
    if len(serials) > 1:
        raise AdbError(
            f"multiple devices online ({serials}); pass an explicit serial"
        )
    return serials[0]


def _getprop(serial: str, key: str) -> str:
    """Best-effort ``getprop``; empty string on failure."""
    try:
        out = _run(
            [adb_bin(), "-s", serial, "shell", "getprop", key],
            timeout=5.0,
        )
        text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
        return text.strip()
    except AdbError:
        return ""


def describe_devices() -> list[dict[str, str]]:
    """List online authorized devices with best-effort display fields.

    Identity is always ADB ``serial``. ``model`` / ``market_name`` are display
    only (empty when getprop fails).
    """
    market_keys = (
        "ro.product.marketname",
        "ro.oppo.market.name",
        "ro.config.marketing_name",
        "ro.vendor.oplus.market.name",
    )
    devices: list[dict[str, str]] = []
    for serial in list_device_serials():
        model = _getprop(serial, "ro.product.model")
        market_name = ""
        for key in market_keys:
            market_name = _getprop(serial, key)
            if market_name:
                break
        devices.append(
            {
                "serial": serial,
                "state": "device",
                "model": model,
                "market_name": market_name,
            }
        )
    return devices


# --- observation -----------------------------------------------------------


def _device_file_size(serial: str | None, path: str) -> int:
    """Return the byte size of `path` on the device, or raise AdbError on failure.

    Uses `adb shell wc -c` because Android's toybox `stat` does not support
    GNU's `-c` format flag. Output looks like `  2048 /sdcard/...xml` (with
    leading whitespace); we parse the first whitespace-delimited token.
    """
    serial_args = ["-s", serial] if serial else []
    try:
        out = _run(
            [adb_bin(), *serial_args, "shell", "wc", "-c", path],
            timeout=_DUMP_WC_TIMEOUT,
        )
    except AdbError as exc:
        msg = str(exc).lower()
        if "no such file" in msg or "cannot open" in msg:
            raise AdbError(
                f"uiautomator dump produced no file (path={path})"
            ) from exc
        raise
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
    parts = text.strip().split()
    if not parts or not parts[0].isdigit():
        raise AdbError(f"wc returned unexpected output for {path}: {text!r}")
    return int(parts[0])


def uiautomator_dump(
    serial: str | None = None,
    *,
    max_attempts: int = _DUMP_MAX_ATTEMPTS,
    min_bytes: int = _DUMP_MIN_BYTES,
) -> str:
    """Dump the accessibility tree via `uiautomator dump` and return XML text.

    Dumps to a fixed device path, pulls it to a local temp file, and returns
    the XML contents. No on-device APK is required.

    To eliminate the "stale-tree-on-new-screenshot" failure mode (Android's
    `uiautomator dump` can exit 0 while writing nothing on null-root
    windows, leaving the previous frame's XML on disk), each attempt:
      1. `rm -f` the device-side file (non-fatal if it fails),
      2. trigger the dump,
      3. verify the dump wrote a non-trivial file via `wc -c`,
      4. only then pull and return.

    Transient null-root failures are absorbed by retrying up to
    `max_attempts` rounds with a short backoff between attempts; terminal
    failure raises `AdbError` carrying the most recent reason — never
    silently returns the previous frame's XML.

    Callers that own their own retry loop (Frame Gate in `get_frame`)
    SHOULD pass ``max_attempts=1`` so backoff lives outside this helper.
    """
    if max_attempts < 1:
        raise AdbError("uiautomator dump max_attempts must be >= 1")
    serial_args = ["-s", serial] if serial else []
    dump_cmd = [adb_bin(), *serial_args, "shell", "uiautomator", "dump", _DEVICE_DUMP_PATH]
    pull_cmd = [adb_bin(), *serial_args, "pull", _DEVICE_DUMP_PATH]

    last_exc: AdbError | None = None
    for attempt in range(max_attempts):
        # 1. Clear the device-side file so a failed dump cannot pull stale.
        # rm is best-effort: ENOENT (file already absent) and shell quoting
        # edge cases must not block the dump itself.
        try:
            _run(
                [adb_bin(), *serial_args, "shell", "rm", "-f", _DEVICE_DUMP_PATH],
                timeout=_DUMP_RM_TIMEOUT,
            )
        except AdbError:
            pass

        # 2. Trigger the dump.
        try:
            _run(dump_cmd, timeout=30.0)
        except AdbError as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                time.sleep(_DUMP_BACKOFF_S)
            continue

        # 3. Verify the dump actually wrote a non-trivial file.
        try:
            size = _device_file_size(serial, _DEVICE_DUMP_PATH)
        except AdbError as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                time.sleep(_DUMP_BACKOFF_S)
            continue
        if size < min_bytes:
            last_exc = AdbError(
                f"uiautomator dump wrote {size} bytes (min {min_bytes}); "
                f"refusing to pull stale device-side content"
            )
            if attempt + 1 < max_attempts:
                time.sleep(_DUMP_BACKOFF_S)
            continue

        # 4. Pull and return.
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
            local_path = Path(tf.name)
        try:
            _run([*pull_cmd, str(local_path)], timeout=30.0)
            return local_path.read_text(encoding="utf-8")
        finally:
            try:
                local_path.unlink(missing_ok=True)
            except OSError:
                pass

    raise last_exc or AdbError("uiautomator dump failed after retries")


def screencap(serial: str | None = None) -> bytes:
    """Capture the screen as PNG bytes via `adb exec-out screencap -p`."""
    serial_args = ["-s", serial] if serial else []
    # exec-out streams raw bytes to stdout without \r translation.
    out = _run(
        [adb_bin(), *serial_args, "exec-out", "screencap", "-p"],
        capture_bytes=True,
        timeout=30.0,
    )
    if not isinstance(out, (bytes, bytearray)):
        raise AdbError("screencap returned non-bytes output")
    return bytes(out)


# --- actions ---------------------------------------------------------------


def input_tap(serial: str | None, x: float, y: float) -> None:
    """Tap at screen coordinates."""
    _run([adb_bin(), *(["-s", serial] if serial else []), "shell", "input", "tap", str(int(x)), str(int(y))])


def input_swipe(serial: str | None, x1: float, y1: float, x2: float, y2: float, duration_ms: int) -> None:
    """Swipe between two points over duration_ms."""
    _run([
adb_bin(), *(["-s", serial] if serial else []), "shell", "input", "swipe",
        str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(duration_ms)),
    ])


def input_text(serial: str | None, text: str) -> None:
    """Type text into the focused field. Whitespace tokens are shell-escaped.

    **ASCII-only.** Non-ASCII / control characters crash Android's
    `InputShellCommand.sendText` (NPE on a null token array). Use
    `input_text_via_broadcast` (ADBKeyBoard) for those payloads instead.
    """
    # adb input text splits on whitespace; replace spaces with %s (Android input convention).
    # `adb shell` joins its remaining argv into one remote shell command, so
    # subprocess list arguments alone do not protect metacharacters such as
    # `|`, `&`, `$`, or quotes. Quote the one user-controlled remote argument.
    safe = shlex.quote(text.replace(" ", "%s"))
    _run([adb_bin(), *(["-s", serial] if serial else []), "shell", "input", "text", safe])


def ime_list_enabled(serial: str | None = None) -> list[str]:
    """Return the list of currently-enabled IME ids via `adb shell ime list -s`.

    Each line is a fully-qualified IME id of the form
    `com.example.ime/.SomeService`. Empty result means no IME is enabled
    (the device has never had an IME switched).
    """
    out = _run([
adb_bin(), *(["-s", serial] if serial else []),
        "shell", "ime", "list", "-s",
    ], timeout=10.0)
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
    return [line.strip() for line in text.splitlines() if line.strip()]


def ime_set(serial: str | None, ime_id: str) -> None:
    """Enable `ime_id` and switch the foreground IME to it.

    `adb shell ime enable <id>` is idempotent and produces no-op output on the
    second call. `adb shell ime set <id>` requires the IME to already be
    enabled; calling it on an absent IME surfaces `Error: selected IME not
    available` which `_run` raises as `AdbError`.
    """
    serial_args = *(["-s", serial] if serial else []),
    _run([adb_bin(), *serial_args, "shell", "ime", "enable", ime_id], timeout=10.0)
    _run([adb_bin(), *serial_args, "shell", "ime", "set", ime_id], timeout=10.0)


def input_text_via_broadcast(serial: str | None, text: str) -> None:
    """Send UTF-8 text via the ADBKeyBoard broadcast intent.

    The text is delivered to whichever app is focused as if the user typed it
    on the ADBKeyBoard IME. ADBKeyBoard must already be installed and selected
    as the foreground IME; otherwise the broadcast is silently dropped (the
    OS does not raise an error on `am broadcast` to an unknown receiver).

    The `--es msg` extra carries the text as a Java `String`. The value is
    quoted for the remote Android shell before ADB joins the command, making
    CJK, emoji, whitespace, and shell metacharacters one intent-extra value.
    """
    # `am broadcast` exits with 0 even if no receiver catches the intent,
    # so we cannot rely on returncode alone — callers must pre-verify IME.
    _run([
adb_bin(), *(["-s", serial] if serial else []),
        "shell", "am", "broadcast",
        "-a", "ADB_INPUT_TEXT",
        "--es", "msg", shlex.quote(text),
    ], timeout=10.0)


def clear_text_via_broadcast(serial: str | None) -> None:
    """Clear the focused editable field through the selected ADBKeyBoard IME."""
    _run([
        adb_bin(), *(["-s", serial] if serial else []),
        "shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT",
    ], timeout=10.0)


def input_keyevent(serial: str | None, keycode: int | str) -> None:
    """Press a keyevent (e.g. 4=back, 3=home, 66=enter)."""
    _run([adb_bin(), *(["-s", serial] if serial else []), "shell", "input", "keyevent", str(keycode)])


def input_keyevent_longpress(serial: str | None, keycode: int | str) -> None:
    """Press a long-press keyevent via `adb shell input keyevent --longpress`.

    Not all Android versions / devices honor `--longpress`; callers should
    catch exceptions and fall back to `input_keyevent`.
    """
    _run([
        adb_bin(), *(["-s", serial] if serial else []),
        "shell", "input", "keyevent", "--longpress", str(keycode),
    ])


def am_start(serial: str | None, app: str) -> None:
    """Launch an app by package via monkey launcher intent (no activity name needed)."""
    _run([
adb_bin(), *(["-s", serial] if serial else []), "shell",
        "monkey", "-p", app, "-c", "android.intent.category.LAUNCHER", "1",
    ])


def list_packages(serial: str | None = None) -> list[str]:
    """Return all installed package names via `pm list packages`."""
    out = _run(
        [adb_bin(), *(["-s", serial] if serial else []), "shell", "pm", "list", "packages"],
        timeout=30.0,
    )
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
    pkgs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkgs.append(line[len("package:"):].strip())
    return pkgs


def wait_ms(duration_ms: int) -> None:
    """Block the driver process for duration_ms."""
    time.sleep(duration_ms / 1000.0)


def _parse_current_activity_identity(text: str) -> dict[str, object]:
    """Parse Android's authoritative resumed-Activity fields."""
    import re

    empty: dict[str, object] = {
        "package": "",
        "activity": "",
        "component": "",
        "sources": [],
        "conflict": False,
    }
    matches = re.findall(
        r"(mResumedActivity|topResumedActivity|ResumedActivity)[^A-Za-z]*"
        r"ActivityRecord\{[^}]*?([A-Za-z_][\w.$]*/[\w.$]+)",
        text,
    )
    if not matches:
        return empty
    # Multi-window Android may expose more than one mResumedActivity.  When
    # topResumedActivity is present it is the exact top-resumed authority and
    # secondary resumed records must not manufacture an identity conflict.
    # HyperOS emits the same task-supervisor fact as ResumedActivity (without
    # the historical "m" prefix), so it participates only when no explicit
    # top-resumed field exists.
    authoritative = [match for match in matches if match[0] == "topResumedActivity"]
    selected = authoritative or matches
    sources: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source, component in selected:
        key = (source, component)
        if key in seen:
            continue
        seen.add(key)
        package, activity = component.split("/", 1)
        sources.append({
            "source": source,
            "component": component,
            "package": package,
            "activity": activity,
        })
    primary = sources[0]
    packages = {item["package"] for item in sources if item["package"]}
    return {
        "package": primary["package"],
        "activity": primary["activity"],
        "component": primary["component"],
        "sources": sources,
        "conflict": len(packages) > 1,
    }


def current_activity_identity(
    serial: str | None = None,
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    """Return exact resumed-Activity identity from deterministic OS facts."""
    try:
        out = _run([
            adb_bin(), *(["-s", serial] if serial else []),
            "shell", "dumpsys", "activity", "activities",
        ], timeout=max(0.05, float(timeout)))
    except AdbError:
        return _parse_current_activity_identity("")
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
    return _parse_current_activity_identity(text)


def current_activity(serial: str | None = None) -> str:
    """Return the activity name from the exact resumed-Activity identity."""
    return str(current_activity_identity(serial).get("activity") or "")


def screen_interactive(serial: str | None = None) -> bool | None:
    """Return whether Android currently accepts normal user input.

    ``None`` means the power state could not be determined reliably.  Callers
    must treat that as unknown rather than guessing: an unnecessary gesture on
    an already-awake device can mutate the foreground app.
    """
    import re

    try:
        out = _run([
            adb_bin(), *(["-s", serial] if serial else []),
            "shell", "dumpsys", "power",
        ], timeout=10.0)
    except AdbError:
        return None
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)

    # This is the most direct signal and is present on most Android versions.
    match = re.search(r"\bmInteractive\s*[=:]\s*(true|false)\b", text, re.IGNORECASE)
    if match:
        return match.group(1).lower() == "true"

    # Fall back to older/newer dumpsys variants.  DREAMING and an ON display
    # are deliberately considered interactive so wake_and_unlock stays inert.
    match = re.search(r"\bmWakefulness\s*[=:]\s*(\w+)\b", text, re.IGNORECASE)
    if match:
        state = match.group(1).lower()
        if state in {"awake", "dreaming"}:
            return True
        if state in {"asleep", "dozing"}:
            return False

    match = re.search(r"\bDisplay Power:\s*state\s*=\s*(\w+)\b", text, re.IGNORECASE)
    if match:
        state = match.group(1).lower()
        if state == "on":
            return True
        if state in {"off", "doze", "doze_suspend"}:
            return False
    return None


def dismiss_keyguard(serial: str | None = None) -> None:
    """Ask Android to dismiss a non-secure keyguard without a touch gesture."""
    _run([
        adb_bin(), *(["-s", serial] if serial else []),
        "shell", "wm", "dismiss-keyguard",
    ], timeout=10.0)


def input_method_diagnostics(serial: str | None = None, *, timeout: float = 0.35) -> dict:
    """Return a tiny, parsed IME diagnostic; never expose the raw dumpsys body."""
    import re

    out = _run(
        [adb_bin(), *(['-s', serial] if serial else []), "shell", "dumpsys", "input_method"],
        timeout=max(0.05, timeout),
    )
    text = out.decode("utf-8", "replace") if isinstance(out, (bytes, bytearray)) else str(out)
    matches = re.findall(
        r"(?:mInputShown|isInputViewShown|mIsInputViewShown)\s*[=:]\s*(true|false)",
        text,
        flags=re.IGNORECASE,
    )
    visible = None if not matches else matches[-1].lower() == "true"
    return {"keyboard_visible": visible, "source": "adb_ime", "confidence": 0.8 if matches else 0.0}


# --- async wrappers (offload blocking kernels to a thread executor) ----------
#
# These keep the sync kernels above intact (tests call them directly) while
# exposing an async surface that never blocks the event loop. Every kernel
# enforces its own `timeout`, so a hung adb surfaces as `AdbError` even when
# run via `to_thread`.


async def uiautomator_dump_async(
    serial: str | None = None,
    *,
    max_attempts: int = _DUMP_MAX_ATTEMPTS,
    min_bytes: int = _DUMP_MIN_BYTES,
    timeout: float | None = None,
) -> str:
    """Dump UI state, using cancellable subprocesses when a budget is supplied."""
    if timeout is None:
        return await asyncio.to_thread(
            uiautomator_dump, serial,
            max_attempts=max_attempts, min_bytes=min_bytes,
        )
    if max_attempts < 1:
        raise AdbError("uiautomator dump max_attempts must be >= 1")
    deadline = time.monotonic() + max(0.001, timeout)
    serial_args = ["-s", serial] if serial else []

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise AdbError("adb command timed out: uiautomator_dump")
        return value

    last_exc: AdbError | None = None
    for attempt in range(max_attempts):
        try:
            await _run_async(
                [adb_bin(), *serial_args, "shell", "rm", "-f", _DEVICE_DUMP_PATH],
                timeout=min(remaining(), _DUMP_RM_TIMEOUT),
            )
        except AdbError:
            pass
        try:
            await _run_async(
                [adb_bin(), *serial_args, "shell", "uiautomator", "dump", _DEVICE_DUMP_PATH],
                timeout=remaining(),
            )
            size_out = await _run_async(
                [adb_bin(), *serial_args, "shell", "wc", "-c", _DEVICE_DUMP_PATH],
                timeout=min(remaining(), _DUMP_WC_TIMEOUT),
            )
            parts = size_out.decode("utf-8", "replace").strip().split()
            if not parts or not parts[0].isdigit() or int(parts[0]) < min_bytes:
                raise AdbError("uiautomator dump produced incomplete output")
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
                local_path = Path(tf.name)
            try:
                await _run_async(
                    [adb_bin(), *serial_args, "pull", _DEVICE_DUMP_PATH, str(local_path)],
                    timeout=remaining(),
                )
                return local_path.read_text(encoding="utf-8")
            finally:
                local_path.unlink(missing_ok=True)
        except AdbError as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                await asyncio.sleep(min(_DUMP_BACKOFF_S, max(0.0, remaining())))
    raise last_exc or AdbError("uiautomator dump failed after retries")


async def screencap_async(serial: str | None = None, *, timeout: float | None = None) -> bytes:
    """Capture PNG bytes, using a cancellable subprocess when budgeted."""
    if timeout is None:
        return await asyncio.to_thread(screencap, serial)
    serial_args = ["-s", serial] if serial else []
    out = await _run_async(
        [adb_bin(), *serial_args, "exec-out", "screencap", "-p"],
        timeout=timeout,
    )
    if not out:
        raise AdbError("screencap returned empty output")
    return bytes(out)


async def input_tap_async(serial: str | None, x: float, y: float) -> None:
    """Async wrapper around `input_tap`."""
    await asyncio.to_thread(input_tap, serial, x, y)


async def input_swipe_async(
    serial: str | None, x1: float, y1: float, x2: float, y2: float, duration_ms: int
) -> None:
    """Async wrapper around `input_swipe`."""
    await asyncio.to_thread(input_swipe, serial, x1, y1, x2, y2, duration_ms)


async def input_text_async(serial: str | None, text: str) -> None:
    """Async wrapper around `input_text`."""
    await asyncio.to_thread(input_text, serial, text)


async def ime_list_enabled_async(serial: str | None = None) -> list[str]:
    """Async wrapper around `ime_list_enabled`."""
    return await asyncio.to_thread(ime_list_enabled, serial)


async def ime_set_async(serial: str | None, ime_id: str) -> None:
    """Async wrapper around `ime_set`."""
    await asyncio.to_thread(ime_set, serial, ime_id)


async def input_text_via_broadcast_async(serial: str | None, text: str) -> None:
    """Async wrapper around `input_text_via_broadcast`."""
    await asyncio.to_thread(input_text_via_broadcast, serial, text)


async def clear_text_via_broadcast_async(serial: str | None) -> None:
    """Async wrapper around `clear_text_via_broadcast`."""
    await asyncio.to_thread(clear_text_via_broadcast, serial)


async def input_keyevent_async(serial: str | None, keycode: int | str) -> None:
    """Async wrapper around `input_keyevent`."""
    await asyncio.to_thread(input_keyevent, serial, keycode)


async def screen_interactive_async(serial: str | None = None) -> bool | None:
    """Async wrapper around :func:`screen_interactive`."""
    return await asyncio.to_thread(screen_interactive, serial)


async def dismiss_keyguard_async(serial: str | None = None) -> None:
    """Async wrapper around :func:`dismiss_keyguard`."""
    await asyncio.to_thread(dismiss_keyguard, serial)


async def input_keyevent_longpress_async(serial: str | None, keycode: int | str) -> None:
    """Async wrapper around `input_keyevent_longpress`."""
    await asyncio.to_thread(input_keyevent_longpress, serial, keycode)


async def am_start_async(serial: str | None, app: str) -> None:
    """Async wrapper around `am_start`."""
    await asyncio.to_thread(am_start, serial, app)


async def list_packages_async(serial: str | None = None) -> list[str]:
    """Async wrapper around `list_packages`."""
    return await asyncio.to_thread(list_packages, serial)


async def require_single_device_async() -> str:
    """Async wrapper around `require_single_device` (used by `health()`)."""
    return await asyncio.to_thread(require_single_device)


async def sole_online_device_async() -> str:
    """Async wrapper around `sole_online_device`."""
    return await asyncio.to_thread(sole_online_device)


async def list_device_serials_async() -> list[str]:
    """Async wrapper around `list_device_serials`."""
    return await asyncio.to_thread(list_device_serials)


async def describe_devices_async() -> list[dict[str, str]]:
    """Async wrapper around `describe_devices`."""
    return await asyncio.to_thread(describe_devices)


async def wait_ms_async(duration_ms: int) -> None:
    """Async wrapper around `wait_ms` (sleep off the event loop)."""
    await asyncio.to_thread(wait_ms, duration_ms)


async def current_activity_async(serial: str | None = None) -> str:
    """Async wrapper around `current_activity`."""
    return await asyncio.to_thread(current_activity, serial)


async def current_activity_identity_async(
    serial: str | None = None,
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    """Read resumed-Activity facts through a cancellable, reaped subprocess."""
    try:
        out = await _run_async(
            [
                adb_bin(),
                *_serial_args(serial),
                "shell",
                "dumpsys",
                "activity",
                "activities",
            ],
            timeout=max(0.05, float(timeout)),
        )
    except AdbTimeoutError as exc:
        identity = _parse_current_activity_identity("")
        identity.update({"timed_out": True, "error": str(exc)})
        return identity
    except AdbError as exc:
        identity = _parse_current_activity_identity("")
        identity.update({"timed_out": False, "error": str(exc)})
        return identity
    identity = _parse_current_activity_identity(out.decode("utf-8", "replace"))
    identity.update({"timed_out": False, "error": ""})
    return identity


async def input_method_diagnostics_async(
    serial: str | None = None, *, timeout: float = 0.35
) -> dict:
    return await asyncio.to_thread(input_method_diagnostics, serial, timeout=timeout)
