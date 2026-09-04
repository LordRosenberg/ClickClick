"""Capture low-risk App screens and list tree-grounded coordinate candidates."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from agent.action_observation import ActionObservationTransaction
from driver.factory import get_driver
from driver.scrcpy_mirror import REGISTRY
from perception.observation import ObservationBuilder
from shared.config import get_settings
from shared.schemas import Action


async def run(serial: str, packages: list[str]) -> dict[str, Any]:
    driver = get_driver(get_settings(), serial=serial)
    transaction = ActionObservationTransaction(
        driver, ObservationBuilder(),
    )
    results: list[dict[str, Any]] = []
    try:
        for package_name in packages:
            before = await transaction.observe_current(
                attach_image=True,
                source="coordinate_profile_fixture",
            )
            launch, observation = await transaction.act_and_observe(
                Action(type="launch", app=package_name), before,
            )
            results.append({
                "requested_package": package_name,
                "launch_success": launch.success,
                "foreground_app_id": observation.ui.app_id,
                "activity": observation.ui.activity,
                "mode": observation.mode.value,
                "accepted": observation.accepted,
                "capture_meta": observation.capture_meta,
                "frame_size": [observation.frame_width, observation.frame_height],
                "elements": [
                    {
                        "index": element.index,
                        "role": element.role,
                        "text": element.text,
                        "desc": element.desc,
                        "bounds": element.bounds,
                        "resource_id": element.resource_id,
                    }
                    for element in observation.ui.elements
                ],
            })
    finally:
        closer = getattr(driver, "close_observation_provider", None)
        if callable(closer):
            await closer()
        await REGISTRY.shutdown()
    return {"serial": serial, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("packages", nargs="+")
    args = parser.parse_args()
    print(json.dumps(
        asyncio.run(run(args.serial, list(args.packages))),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
