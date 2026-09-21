---
name: androidworld-camera-controls
description: AndroidWorld API 33 系统相机的模式菜单、拍照／摄像选择、按任务要求切换摄像头，以及底部开始和停止控件。
version: 1.3.2
app_aliases: [Camera, 相机]
app: com.android.camera2
device_profiles: [androidworld_api33]
kind: app_core
capability: camera_controls
source: authored
---

# AndroidWorld camera controls

## Hints

- From the Camera live preview, when the photo/video mode chooser is hidden,
  request it with the existing action `{"type":"key","key":"menu"}` through
  `submit_executor_step` (decision `act`, current observation_id and summary).
  The driver maps `menu` to Android keycode 82, equivalent to
  `adb shell input keyevent KEYCODE_MENU`. This is one normal device action;
  no shell tool, host-specific ADB path or hard-coded device serial is needed.
- On launch/resume, this camera briefly flashes its photo/video chooser and
  then hides it. The launch observation may catch that flash, including the
  accessibility label `Switch to Video Camera`; it is not a durable tap target.
  Let the launch transition finish with `observe_screen`, then deliberately
  open the chooser with Menu when hidden. Select `Video` from that fresh
  observation. An already visible chooser is usable when explicitly opened
  in this interaction, not merely glimpsed during launch.
  A successful key dispatch alone does not establish that the chooser opened.
  If absent, inspect the current UI state before retrying or changing approach.
- A left-to-right swipe across the preview is an alternative, but repeated
  tests on this emulator showed intermittent failure even with identical
  coordinates and duration. Do not assume a swipe opened the chooser or use
  a leftward swipe as a video-mode substitute; that can open the media filmstrip.
  Choose `Camera` for photos or `Video` for recording, then confirm the mode.
- The photo shutter and video start/stop controls are at the bottom of the
  camera screen. The mode chooser selects photo or video mode; selecting a mode
  does not itself take a photo or start recording.
- After selecting Video, require the resulting bottom shutter to have the
  video-camera glyph/video-mode appearance before pressing it. A label in the
  old chooser, a successful tap, or the chooser disappearing does not prove
  the mode changed. If the still-camera glyph remains, stay in the app and
  reopen Menu; do not probe the shutter or relaunch Camera to check the mode.
- For ordinary photo/video requests, keep the current camera. Switch front/back
  only when the task requests a specific facing camera and the current one differs.
  Open `Options` (the three dots beside the preview), then use the labeled
  camera toggle (`com.android.camera2:id/camera_toggle_button`). This changes
  front/back facing, not photo/video mode. Both cameras can show a synthetic
  test scene; do not infer camera facing from that scene. Use the control's
  current label and verify the requested photo/video mode remains selected.
- For a photo, use the bottom shutter in photo mode. For a video, use the bottom
  recording control to start, confirm recording has started, and use the bottom
  stop control when the requested recording is finished. Confirm recording has
  stopped before reporting the video saved.
- For an unspecified video duration, allow a short nonzero recording interval
  (for example two seconds) before stopping. A `00:00` timer alone proves no
  useful duration; confirm that it advances. Preserve budget for both start and
  stop. After stopping, confirm a new video thumbnail/playback entry if available;
  disappearance of the timer alone does not prove a video file was saved.
- After pressing Stop, use `observe_screen` to check the settled result before
  pressing that shared control again. A lingering timer can be a saving/transition
  frame; another tap may start a second recording. Confirm the current mode first.
  If no saved-video evidence appears, report that uncertainty instead of claiming
  success from a timer alone. Avoid spending the remaining budget exploring
  unrelated filmstrip menus; a blank item is not video evidence.
- Locate these controls from the current screenshot and accessibility tree.
  If the tree omits a control, inspect the bottom of the current image rather
  than treating the preview or the mode option as the shutter. Re-read the screen
  after switching modes; do not reuse coordinates from the mode chooser.
- When an exact photo/video count is requested, inspect the resulting capture
  or recording state before pressing a capture control again. A dispatched tap
  alone is not proof of a saved photo or video.
- After a photo shutter press, a grey/disabled shutter can mean capture is still
  pending. Use `observe_screen` to inspect the resulting state before leaving the
  app. Prefer the camera's new thumbnail/filmstrip for a local result check. If an
  external gallery is blocked by onboarding, do not cycle between it and Camera;
  use a supported local-file check or report the unresolved capture. Check the
  camera photo folder, not just Downloads. Retry a capture only after establishing
  that the prior attempt produced no photo; an unavailable check is not absence.

These controls describe the AndroidWorld API 33 camera, not other camera apps
or device layouts.
