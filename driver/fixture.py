"""In-memory fixture driver for unit/integration tests without a phone."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

from perception.normalizer import normalize_a11y_tree
from shared.app_resolver import normalize_app_query
from shared.schemas import (
    Action,
    ActionResult,
    AppResolutionProvenance,
    AppResolutionResult,
    AppResolutionStatus,
    CanonicalUI,
)


DEFAULT_TREE: dict[str, Any] = {
    "package": "com.example.demo",
    "activity": ".MainActivity",
    "tree": {
        "class": "FrameLayout",
        "bounds": [0, 0, 1080, 2400],
        "children": [
            {
                "class": "android.widget.Button",
                "text": "Play",
                "clickable": True,
                "bounds": [100, 200, 400, 300],
            },
            {
                "class": "android.widget.EditText",
                "text": "",
                "editable": True,
                "clickable": True,
                "bounds": [100, 400, 900, 500],
            },
            {
                "class": "android.widget.TextView",
                "text": "Demo App",
                "clickable": False,
                "bounds": [100, 80, 500, 140],
            },
        ],
    },
}


def _blank_png(w: int = 1080, h: int = 2400) -> bytes:
    """Return a PNG that does NOT trigger the cv gap detector.

    The historical `_blank_png` was a 1080×2400 solid light-grey field,
    which the cv `low_complexity_visual` gap rule
    (decouple-cv-from-gap-and-add-low-complexity-rule) interprets as a
    pure-color background and would always fire — even on test fixtures
    that pre-date the rule. To keep the fixture's "normal screen"
    semantics, we fill the canvas with a checkerboard of contrasting
    blocks (the 8-px block size survives the 1/8 downscale + 7×7
    boxFilter std-map used by `_low_complexity_uncovered_ratio`).
    The result has high local std everywhere, so the rule stays silent
    unless an explicit test forces the low-complexity path."""
    img = Image.new("RGB", (w, h), color=(245, 245, 245))
    draw = ImageDraw.Draw(img)
    px = img.load()
    for y in range(0, h, 16):
        for x in range(0, w, 16):
            if (x // 16 + y // 16) % 2:
                for dy in range(16):
                    for dx in range(16):
                        if x + dx < w and y + dy < h:
                            px[x + dx, y + dy] = (200, 200, 200)
    draw.rectangle([100, 200, 400, 300], outline=(0, 0, 0), width=2)
    draw.text((120, 230), "Play", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FixtureDriver:
    """Deterministic DeviceDriver for tests."""

    serial: str = "fixture"

    def __init__(self, tree: dict[str, Any] | None = None) -> None:
        self.tree = tree or DEFAULT_TREE
        self.actions: list[Action] = []
        self.task_sessions: list[tuple[str, str]] = []
        self.environment_initializations = 0
        self._png = _blank_png()

    async def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "platform": "android",
            "serial": self.serial,
            "portal_reachable": True,
        }

    async def initialize_environment(self) -> dict[str, Any]:
        self.environment_initializations += 1
        return {"serial": self.serial, "status": "ready", "steps": {}}

    async def reconcile_environment(self) -> dict[str, Any]:
        return await self.initialize_environment()

    async def readiness(self) -> dict[str, Any]:
        return {"status": "ready", "collector": {"ready": True}}

    async def get_ui_state(self) -> CanonicalUI:
        payload = self.tree
        tree = payload.get("tree", payload)
        if isinstance(tree, dict):
            tree = dict(tree)
            capture = dict(tree.get("_capture") or {})
            capture.update({"provider": "fixture", "complete": True})
            tree["_capture"] = capture
        return normalize_a11y_tree(
            tree,
            app_id=str(payload.get("package", "")),
            activity=str(payload.get("activity", "")),
        )

    async def screenshot(self) -> bytes:
        return self._png

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        payload = self.tree
        tree = payload.get("tree", payload)
        if isinstance(tree, dict):
            tree = dict(tree)
            tree.setdefault("package", str(payload.get("package", "")))
            capture = dict(tree.get("_capture") or {})
            capture.update({
                "provider": "fixture",
                "complete": True,
                "coordinate_compatible": True,
                "frame_geometry": [1080, 2400],
            })
            tree["_capture"] = capture
            return tree, self._png
        return {
            "class": "Root",
            "children": [],
            "_capture": {
                "provider": "fixture",
                "complete": True,
                "coordinate_compatible": True,
                "frame_geometry": [1080, 2400],
            },
        }, self._png

    async def current_activity(self) -> str:
        return str(self.tree.get("activity", ""))

    async def current_foreground_identity(
        self, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        package = str(self.tree.get("package", ""))
        activity = str(self.tree.get("activity", ""))
        component = f"{package}/{activity}" if package and activity else ""
        return {
            "package": package,
            "activity": activity,
            "component": component,
            "sources": ([{"source": "fixture", "component": component,
                          "package": package, "activity": activity}]
                        if component else []),
            "conflict": False,
        }

    async def resolve_installed_app(self, app: str) -> dict[str, Any]:
        package = str(self.tree.get("package") or "com.example.demo")
        requested = (app or "").strip()
        folded = normalize_app_query(requested)
        aliases = {normalize_app_query("Demo App"), normalize_app_query(package)}
        resolved = folded in aliases
        return AppResolutionResult(
            requested_name=requested,
            normalized_query=folded,
            status=(AppResolutionStatus.RESOLVED if resolved else AppResolutionStatus.MISS),
            resolver_generation="fixture:v1",
            package=package if resolved else None,
            provenance=(
                AppResolutionProvenance.EXACT_PACKAGE
                if requested == package else AppResolutionProvenance.CURATED_ALIAS
            ) if resolved else None,
        ).model_dump(mode="json")

    async def search_installed_apps(self, query: str, *, limit: int = 12) -> list[dict[str, str]]:
        package = str(self.tree.get("package") or "com.example.demo")
        folded = normalize_app_query(query)
        if folded and folded not in normalize_app_query("Demo App " + package):
            return []
        return [{
            "package": package,
            "alias": "Demo App",
            "provenance": "curated_alias",
        }][:limit]

    async def validate_installed_app(self, package: str) -> bool:
        return package == str(self.tree.get("package") or "com.example.demo")

    async def record_installed_app_selection(self, display_name: str, package: str) -> bool:
        return bool(display_name and await self.validate_installed_app(package))

    async def wake_and_unlock(self) -> None:
        # Fixture driver: nothing to wake. Record nothing; never raise.
        return None

    async def begin_task_session(self, _task_id: str) -> dict[str, Any]:
        self.task_sessions.append(("begin", _task_id))
        return {"status": "disabled", "reason": "fixture_driver"}

    async def end_task_session(self, _task_id: str = "") -> dict[str, Any]:
        self.task_sessions.append(("end", _task_id))
        return {"status": "not_active"}

    async def act(self, action: Action) -> ActionResult:
        self.actions.append(action)
        # Fixture driver: accept all action types; surface a failure only for
        # clearly invalid inputs (missing required fields). Name resolution
        # is stubbed — `launch` with a display name returns a failure so
        # tests can exercise the ambiguous-name path.
        if action.type == "launch" and action.app and "." not in action.app:
            return ActionResult(success=False, message=f"fixture: unresolved app name '{action.app}'",
                                  detail={"type": action.type})
        if action.type == "scroll" and not action.direction:
            return ActionResult(success=False, message="fixture: scroll missing direction",
                                  detail={"type": action.type})
        detail = {"type": action.type}
        if action.type == "launch" and action.app:
            detail["resolved_package"] = action.app
        return ActionResult(success=True, message="fixture-ok", detail=detail)
