"""Prepare Android 14 PhotoPicker's synthetic directories after snapshot restore.

Only MediaProvider/FUSE directory lookups are performed. No image, selection,
permission, app data, or answer is supplied or modified.
"""

from __future__ import annotations

import subprocess
from typing import Any


def prepare_photo_picker(target: Any, apps: list[str]) -> dict[str, Any]:
    if "Mattermost" not in apps:
        return {"required": False, "ready": True}
    command = ["docker", "exec", target.container, "adb", "-s", target.environment_device]
    try:
        sdk = subprocess.check_output(
            [*command, "shell", "getprop", "ro.build.version.sdk"], timeout=10,
        ).decode().strip()
        if not sdk.isdecimal():
            raise ValueError("Unrecognized Android SDK")
        # This is the observed Android 14 snapshot issue, not a provider upgrade.
        if sdk != "34":
            return {"required": False, "ready": True, "sdk": sdk}
        user = subprocess.check_output(
            [*command, "shell", "am", "get-current-user"], timeout=10,
        ).decode().strip()
        if not user.isdecimal():
            raise ValueError("Unrecognized current Android user")
        path = f"/storage/emulated/{user}/.transforms/synthetic/picker"
        paths = [path, f"{path}/{user}",
                 f"{path}/{user}/com.android.providers.media.photopicker",
                 f"{path}/{user}/com.android.providers.media.photopicker/media"]
        # A fresh snapshot's first concurrent copy/thumbnail reads can race the
        # provider's directory creation (EFAULT). Resolve each parent serially
        # through FUSE, which creates directories under the provider's identity.
        # Never mkdir/chown the backing /data/media paths as root.
        checked = []
        for path in paths:
            for attempt in range(2):
                result = subprocess.run(
                    [*command, "shell", "stat", "-c", "%F", path],
                    capture_output=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip() == b"directory":
                    checked.append({"path": path, "attempts": attempt + 1})
                    break
            else:
                return {"required": True, "ready": False, "sdk": sdk,
                        "failure": "photo_picker_directory_unavailable", "checked": checked}
        return {"required": True, "ready": True, "sdk": sdk, "checked": checked,
                "scope": "system_photo_picker_parent_directories_only"}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"required": True, "ready": False,
                "failure": "photo_picker_preparation_unavailable"}
