"""Use the device's existing name resolver for package-scoped skill routing."""

import re

from shared.schemas import AppResolutionResult, AppResolutionStatus

_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")


async def skill_app(app: str, driver, *, strict: bool = True) -> str:
    target = (app or "").strip()
    if not target or (not strict and _PACKAGE.fullmatch(target)):
        return target
    resolver = getattr(driver, "resolve_installed_app", None)
    if callable(resolver):
        result = AppResolutionResult.model_validate(await resolver(target))
        if result.status == AppResolutionStatus.RESOLVED and result.package:
            return result.package
    if strict:
        raise ValueError(
            "target_app could not be resolved to an installed app. "
            "Prefer the app name/alias from the request; do not guess a package. "
            "Use an installed package from evidence, or leave it empty and name the app in goal."
        )
    # Old persisted plans may contain unresolved display names. Keep them readable;
    # unknown skill scope uses the observed foreground, never a guessed package.
    return ""
