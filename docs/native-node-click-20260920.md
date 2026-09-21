# Observation-bound native node clicks

The executor previously resolved every `tap(index)` to the center of its basis
observation's bounds. A layout change during model reasoning could therefore
redirect a valid index selection to a different control. This change preserves
the observed Android node for controls advertising `ACTION_CLICK`.

## Scope and execution

- Collector attaches opaque, random handles only to nodes supporting click.
- Handles refer to retained native nodes, not paths or indices in a newer tree.
  Storage is bounded to 16,000 nodes and 120 seconds, pruned on access and every
  20 seconds, and cleared on service restart/destruction.
- Executor validates the observation/index as before, then binds a private
  action handle. Models cannot supply it; action serialization cannot replay it.
- Device consumes the handle before work, refreshes that node exactly once,
  checks window/package/class/resource/text/description/hint and node-type
  attributes, visibility, enabled state and click support, then calls
  `performAction(ACTION_CLICK)` once. Bounds/focus/selection changes are allowed.
  A refresh taking over 1.5 seconds cannot dispatch a late action.
- Host makes one bounded exchange (2.5 seconds). No automatic retry, rebind by
  new index, parent-node substitution, or old-coordinate fallback is permitted
  after selecting this path. Post-action observation remains in the existing
  transaction; node errors also get a fresh screenshot.

Only standalone `tap(index)` is changed. Coordinate taps, long presses, drags,
compound actions and targeted text-entry helper taps retain their existing paths.
Older collectors and nodes without handles retain legacy coordinate resolution.
Only the in-process Android driver advertises support; legacy HTTP transports
keep coordinate resolution so serialization cannot silently lose a binding.
`CLICKCLICK_NODE_CLICK_ENABLED=0` restores legacy executor resolution. This is
an explicit rollback option, never an automatic fallback after an uncertain click.

## Feedback and limitations

Receipts expose `node_click_status` and `native_action_performed`, separately
from effect outcome. `performed` means Android reported handling the action;
it does not prove the selected color, navigation, or business result is correct.
Uncertain transport/action results remain `effect_outcome=unknown` and receive
fresh observation; even a false action return is not a license for blind replay.
Existing event acknowledgement remains unchanged on the coordinate paths.

Disappearance/replacement, identity changes, disabled/hidden nodes and expired
or consumed handles fail without coordinate dispatch. Anonymous nodes reused
in place with identical exposed attributes but changed visual/business meaning
remain a limitation. This is not a universal semantic identity or overlay proof.
Accessibility click behavior can differ from touch behavior in custom widgets;
real app coverage matters. Android 13 Chrome behavior is tested below; other
Android versions/OEM accessibility implementations still need device validation.

The local APK is a development rebuild of Collector 0.4.4, installed with an
explicit path/digest. No published release pin or previously frozen full-test runtime is
changed. The previous installed APK is preserved for rollback.

## Validation

The focused implementation suite passed 160 Python tests covering executor binding, native dispatch, action/observation transactions and persistent transport. Kotlin unit tests and an APK build also passed with JDK 17 and Gradle 8.9.

On an Android API 33 emulator with Chrome, the diagnostic probe covered seven scenarios: stable target, moved target, removed target, replaced target, renamed target, disabled target and consumed-handle replay. Stable and moved targets received exactly one click on the original node; invalidated targets and replay received none. A stale-coordinate control hit a decoy at the old position. A second probe repeated all seven scenarios with anonymous buttons and also passed.

Three later task regressions passed within their original action budgets: BrowserDraw used 17/20 actions, RetroSavePlaylist 35/50 and FilesMoveFile 14/20. Earlier scoped testing had a BrowserDraw failure associated with redundant navigation and insufficient remaining budget; its result was retained separately rather than replaced. These samples establish targeted behavior, not a universal success rate or a general latency improvement.

The reproducible diagnostic entry point is `scripts/probe_native_node_click.py`. Supply an ADB executable through PATH or `--adb`, a device serial and a fresh output directory. Raw diagnostic traces, screenshots and earlier attempt logs are kept outside the public source distribution. The [evaluation guide](evaluation.md) describes how to record a new comparison.
