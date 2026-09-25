---
name: retro-music-playlists
description: Retro Music playlist creation, ordered song insertion, and playing-queue workflows.
version: 1.4.1
app_aliases: [Retro Music, Retro, Retro Player]
role_sections: true
app: code.name.monkey.retromusic
interface_scope: app
kind: app_core
capability: manage_playlists_and_queue
source: authored
---

# Retro Music playlists and queue

## Shared

### Procedure

- Create an empty playlist from the `Playlists` library section and its `New
  playlist` action. Enter the requested title and finish the create dialog.
- To add songs to that newly created playlist, leave the playlist detail and
  switch to the `Songs` library section. Do not use `Add to playlist` inside the
  empty playlist: in this app that action sends the current playlist's songs to
  another playlist; it does not open a picker for adding songs to the current one.
- In `Songs`, locate each requested title, open that song's overflow menu, choose
  `Add to playlist`, and select the destination playlist. Add songs one at a time
  in the requested order because insertion order becomes playlist order. Search
  for an exact or distinctive title when scrolling would be ambiguous.
- For a playing-queue task, also work from `Songs`. Process songs in the requested
  order and use each song's overflow action for adding it to the playing queue.
  Do not create a playlist unless the task explicitly asks for one.

### Duration-constrained playlists

- When the task specifies only a target total duration, inspect enough song
  durations to choose a combination in range before committing the playlist.
  Sequential playback can expose successive durations more cheaply than opening
  every song's Details dialog. Record the title-duration pairs and calculate the
  combination explicitly.
- A duration-only task does not require a particular song order. When durations
  were collected by sequential playback, do not exit the player and navigate
  back to `Songs`. Tap the bottom `Now playing queue` control: that queue retains
  the songs already visited during playback. Long-press one chosen song there,
  select the other chosen songs, then use the top-right `More options` action and
  `Add to playlist` to add the batch to a new or existing playlist.
- When this queue was produced by advancing sequentially through the song list,
  use that known play order to predict where an off-screen title lies. Earlier
  songs are toward the top and later songs toward the bottom. For multi-screen
  navigation, pair with `adaptive-list-traversal`; the observed queue order and
  song titles supply anchors for locating the remaining selected targets.
- If the required songs are not all present in the playing queue, fall back to
  `Songs`: long-press one chosen song, select the others, and use the selection
  action to add them together. Prefer either batch path over creating an empty
  playlist and performing a separate overflow/Add/destination sequence for every
  song.
- Among known valid combinations, prefer one with fewer songs so the batch needs
  fewer selection actions. Do not accept the first valid sum without checking
  whether the measured durations already support a smaller combination.
- During multi-selection, a song row near the bottom of the screen can overlap
  Retro Music's `Albums`/`Artists`/`Playlists` navigation. Before tapping such a
  row, scroll it into the middle of the list and reacquire its current control.
  A tap in the bottom navigation exits selection mode and loses the whole batch.
- Reserve enough actions for selection and the final add/create dialog. Stop
  collecting additional durations once a valid combination is known; a complete
  library inventory is unnecessary.

### Verification

- A playlist title alone does not prove its contents or order. When verification
  is needed and the remaining budget allows it, inspect the playlist detail or
  the Now playing queue using the current visible state. Do not require reopening
  a playlist after every successful insertion and do not add a mandatory reopen
  step when the task budget is tight.
- A toast or dispatched menu action is evidence only for that one addition. Keep
  the requested order in working memory and proceed to the next song only after
  the current addition has a supported result.

### Anti-patterns

- Do not probe an empty playlist's menu for an inbound song picker.
- Do not batch songs in an order different from the requested order and assume
  they can be rearranged later. This restriction does not apply when the task
  constrains only total duration and imposes no song order.

This skill supplies no playlist titles, song titles, or success verdicts.
Explicit user instructions take precedence.
