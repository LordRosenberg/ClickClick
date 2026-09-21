#!/usr/bin/env bash
# One-shot device bootstrap: install Accessibility Collector + ADBKeyboard,
# then run the same provisioning path as POST /api/devices/{serial}/initialize.
#
# Usage:
#   ./scripts/bootstrap-device.sh              # sole adb device, or pass serial
#   ./scripts/bootstrap-device.sh <serial>
#   ./scripts/bootstrap-device.sh --apk-only   # install APKs only (no settings)
#   ./scripts/bootstrap-device.sh --help
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "${ROOT}" ]]; then
  ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
cd "$ROOT"

if [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  PYTHON_BIN="$ROOT/.venv/Scripts/python.exe"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi

APK_ONLY=0
SERIAL="${CLICKCLICK_DEVICE_SERIAL:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apk-only) APK_ONLY=1; shift ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    -*)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
    *)
      SERIAL="$1"
      shift
      ;;
  esac
done

IME_CACHE_DIR="${CLICKCLICK_DEVICE_APK_CACHE:-$ROOT/data/device-apks}"
IME_APK="${CLICKCLICK_IME_APK_PATH:-$IME_CACHE_DIR/ADBKeyboard.apk}"
IME_URL="${CLICKCLICK_IME_APK_URL:-https://raw.githubusercontent.com/senzhk/ADBKeyBoard/master/ADBKeyboard.apk}"
IME_ID="${CLICKCLICK_IME_ID:-com.android.adbkeyboard/.AdbIME}"

resolve_adb() {
  "$PYTHON_BIN" -c 'from driver.adb import adb_bin; print(adb_bin())'
}

ADB_BIN="$(resolve_adb)"
adb() {
  "$ADB_BIN" "$@"
}

resolve_serial() {
  local devices=()
  local line state serial
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == List* ]] && continue
    serial="${line%%$'\t'*}"
    state="${line##*$'\t'}"
    if [[ "$state" == "device" ]]; then
      devices+=("$serial")
    fi
  done < <(adb devices | sed '1d')

  if [[ -n "$SERIAL" ]]; then
    for serial in "${devices[@]}"; do
      if [[ "$serial" == "$SERIAL" ]]; then
        printf '%s\n' "$SERIAL"
        return 0
      fi
    done
    echo "device not authorized/online: $SERIAL" >&2
    adb devices >&2
    exit 1
  fi

  if [[ "${#devices[@]}" -eq 1 ]]; then
    printf '%s\n' "${devices[0]}"
    return 0
  fi
  if [[ "${#devices[@]}" -eq 0 ]]; then
    echo "no authorized adb devices (enable USB debugging and accept the prompt)" >&2
    adb devices >&2
    exit 1
  fi
  echo "multiple devices; pass a serial:" >&2
  printf '  %s\n' "${devices[@]}" >&2
  exit 1
}

download_file() {
  local url="$1"
  local dest="$2"
  mkdir -p "$(dirname "$dest")"
  echo "downloading $(basename "$dest") ← $url"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 1 -o "$dest.partial" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$dest.partial" "$url"
  else
    echo "need curl or wget to download $url" >&2
    exit 1
  fi
  mv "$dest.partial" "$dest"
}

ensure_collector_apk() {
  COLLECTOR_APK="$("$PYTHON_BIN" -m driver.collector_release --resolve)"
}

ensure_ime_apk() {
  if [[ -f "$IME_APK" ]]; then
    return 0
  fi
  download_file "$IME_URL" "$IME_APK"
}

SERIAL="$(resolve_serial)"
ensure_collector_apk
ensure_ime_apk

echo "serial:    $SERIAL"
echo "collector: $COLLECTOR_APK"
echo "ime:       $IME_APK"

install_apk() {
  local label="$1"
  local apk="$2"
  local package="$3"
  local out
  echo "→ adb install $label"
  set +e
  out="$(adb -s "$SERIAL" install -r -t "$apk" 2>&1)"
  local rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    printf '%s\n' "$out"
    return 0
  fi
  if printf '%s\n' "$out" | grep -q 'INSTALL_FAILED_UPDATE_INCOMPATIBLE'; then
    echo "signature mismatch for $package; uninstalling then reinstalling"
    adb -s "$SERIAL" uninstall "$package" >/dev/null 2>&1 || true
    adb -s "$SERIAL" install -r -t "$apk"
    return 0
  fi
  printf '%s\n' "$out" >&2
  return "$rc"
}

install_apk "collector" "$COLLECTOR_APK" "ai.clickclick.collector"
install_apk "ADBKeyboard" "$IME_APK" "com.android.adbkeyboard"

if [[ "$APK_ONLY" -eq 1 ]]; then
  echo "APKs installed (--apk-only). Run without --apk-only to enable services."
  exit 0
fi

export CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH="$COLLECTOR_APK"
export CLICKCLICK_IME_APK_PATH="$IME_APK"

echo "→ initialize_android_device (merge accessibility + enable IME)"
ROOT="$ROOT" SERIAL="$SERIAL" COLLECTOR_APK="$COLLECTOR_APK" IME_APK="$IME_APK" IME_ID="$IME_ID" \
"$PYTHON_BIN" - <<'PY'
import asyncio
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["ROOT"])
sys.path.insert(0, str(root))

from driver.environment import initialize_android_device

result = asyncio.run(
    initialize_android_device(
        os.environ["SERIAL"],
        collector_apk_path=os.environ["COLLECTOR_APK"],
        ime_id=os.environ["IME_ID"],
        ime_apk_path=os.environ["IME_APK"],
    )
)
print(json.dumps(result, ensure_ascii=False, indent=2))
status = result.get("status")
if status == "failed":
    sys.exit(1)
steps = result.get("steps") or {}
acc = steps.get("accessibility_service") or {}
if acc.get("guidance"):
    print("\noperator action:", acc["guidance"], file=sys.stderr)
if status in {"operator_action_required", "degraded"}:
    print(
        "\nNote: some OEMs still require manually enabling the Accessibility "
        "service and/or allowing restricted settings.",
        file=sys.stderr,
    )
PY

echo
echo "done. Next: start clickclick-driver / clickclick-api and open the Console."
echo "Compatibility: collector needs Android 8.0+ (API 26). ADBKeyboard is a"
echo "generic third-party IME; OEM policy may still block accessibility/IME toggles."
