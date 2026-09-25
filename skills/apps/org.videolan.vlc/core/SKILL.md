---
name: vlc-playlists
description: VLC playlist creation, ordered media insertion, and Android player-control behavior.
version: 1.3.3
app_aliases: [VLC, VLC App, VLC Player]
role_sections: true
app: org.videolan.vlc
interface_scope: app
kind: app_core
capability: manage_playlists_and_player_controls
source: authored
verified_actions:
  - id: vlc.open_and_pause
    template: tap_then_key
    key: media_pause
    purpose: Open the targeted verified media row and pause playback promptly.
  - id: vlc.close_tips_and_pause
    template: tap_then_key
    key: media_pause
    delay_ms: 100
    purpose: Close the visible VLC Video player tips overlay and pause playback again.
---

# VLC playlists and player controls

## Shared

### Procedure

- VLC's empty `Playlists` tab has no direct create control. Start from a media
  item: open its overflow menu and choose `Add to playlist`, or long-press media
  to enter selection mode and then choose `Add to playlist` from selection
  actions.
- For exact task-provided filenames, prefer `Browse` > `Internal memory` >
  `VLCVideos`. The main Video library may group media and its lowest visible row
  can overlap the bottom navigation; use Browse instead of repeatedly tapping an
  uncertain or clipped row.
- The new-playlist dialog's `Playlist name` field is editable but lacks a stable
  resource id in this VLC build. Indexed `replace_text` is therefore unsupported.
  Tap the visible name field to focus it, then use ordinary `type` with the full
  title, and press `SAVE`.
- To preserve a required order, either select all intended media in that order
  before creating the playlist, or create it from the first requested item and
  add each remaining item in order through its overflow menu > `Add to playlist`
  > the existing destination playlist. The second route is safer when rows are
  grouped, clipped, or hard to select reliably.
- For multiple playlists, finish one playlist, return to the media source, then
  repeat the same first-item creation flow for the next title. When adding later
  items, distinguish the existing destination by its exact title and shown media
  count.

### Player controls

- For a short video that may finish during model reasoning, invoke the currently
  available `vlc.open_and_pause` action on the verified media row. Use its current
  index, or coordinates only when no usable index exists. This combines opening
  and pausing into one submitted action.
- VLC may show a `Video player tips` tutorial over the newly opened player. Its
  close button starts or resumes the video without changing Activity. Invoke the
  currently available `vlc.close_tips_and_pause` action on its top-left `X` so
  playback is paused again in the same submitted action. This same-Activity
  exception applies only to the clearly identified
  VLC `Video player tips` close button; never use it for `NEXT`, a permission
  dialog, an ad, or an unrelated overlay.
- VLC hides the title and seek controls during playback. Tap the video or seek
  area to reveal the HUD when the current filename or duration must be checked.
- For reading text or ordered frames in a short video, keep playback paused:
  tapping the timeline updates the displayed frame without pressing Play. Play
  resumes continuous playback and hides the controls; the video can finish and
  advance while the next model response is being generated. Refine a sampled
  interval with another paused timeline position, not a Play/Pause round trip.
- A timeline seek briefly overlays a large timestamp in the center of the video.
  If it covers text being transcribed, keep the same paused position and use
  `observe_screen(mode=snapshot)` to read the unobstructed frame after it fades.
  Another seek recreates the obstruction. Do not guess hidden letters or treat
  the obstructed frame as confirmation of an earlier spelling.
- VLC can automatically advance after a short video ends. After dismissing a
  tutorial overlay or seeing an unexpected frame or elapsed-time jump, reveal the
  HUD and reconfirm the title. Reopen the exact file and pause promptly if it has
  advanced. Timeline sampling and coverage policy belong to the selected generic
  dynamic-video-content-reading skill, not this App core.

### Verification

- Saving the title proves neither the remaining items nor their order. If the
  task needs visible verification and budget remains, use `Playlists` and its
  detail view to inspect title, count, and order. Do not make reopening mandatory
  after every addition or after every playlist when the requested state is
  already supported and the action budget is tight.
- Reacquire controls after dialogs, navigation changes, or selection mode. A
  historical overflow index is not valid for the next media row.

### Anti-patterns

- Do not search the empty `Playlists` tab for a standalone new-playlist button.
- Do not retry indexed `replace_text` on the resource-id-less playlist-name field;
  use separate focus and `type`.

This skill supplies no playlist titles, filenames, or success verdicts.
Explicit user instructions take precedence.
