"""Restore the emulator's captured clock/power/IME settings after evaluation."""

from datetime import datetime, timezone

KEYS = ("global/auto_time", "global/auto_time_zone", "system/screen_off_timeout",
        "global/stay_on_while_plugged_in", "secure/default_input_method")


def capture(adb):
    result = {key: adb("shell", "settings", "get", *key.split("/")).decode().strip()
              for key in KEYS}
    result["timezone"] = adb("shell", "getprop", "persist.sys.timezone").decode().strip()
    return result


def restore(adb, saved):
    adb("shell", "service", "call", "alarm", "3", "s16", saved["timezone"])
    adb("shell", "date", "-u", datetime.now(timezone.utc).strftime("%m%d%H%M%y.%S"))
    for key in KEYS:
        namespace, name = key.split("/")
        value = saved[key]
        if value == "null":
            adb("shell", "settings", "delete", namespace, name)
        else:
            adb("shell", "settings", "put", namespace, name, value)
    adb("shell", "input", "keyevent", "KEYCODE_HOME")
    observed = capture(adb)
    return {"captured": saved, "observed": observed, "match": saved == observed}
