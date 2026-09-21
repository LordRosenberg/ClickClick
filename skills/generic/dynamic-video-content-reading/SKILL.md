---
name: dynamic-video-content-reading
description: Sample a video timeline to read changing visual content or its order. Use for transcription and scene/event sequencing, not static metadata or a single visible frame.
version: 1.0.9
kind: generic
tags: [video, temporal, transcription, sampling]
triggers: [transcribe video, read video content, ordered video frames, scene sequence]
source: authored
---

# Dynamic video content reading

## Boundary

Use temporal sampling only to read changing video content or establish its order,
repetition, occurrence, or coverage. A filename, title, duration, resolution, or
single visible frame is static metadata/current state and needs no sampling.

## Establish control

- Open the exact target, pause promptly, then reveal controls and confirm title and
  duration. Recheck the title after an unexpected frame/time jump, playback end,
  or overlay dismissal because autoplay may have advanced to another video.
- Use an exact currently available App-authorized compound action only when the
  injected App guidance states that it opens or dismisses the current player
  surface and pauses playback promptly; this generic skill grants no authorization.
- Prefer paused seeking. Reasoning is not realtime observation, so unattended
  content may pass unseen. A bounded `observe_screen(mode=sequence)` proves only
  its sampled interval and is for local motion/order, not whole-video coverage.

## Coarse sampling

Start with four timeline anchors: near the beginning, near `T/3`, near `2T/3`,
and just before the end. If the first readable paused frame is near 0, use it as the beginning
anchor; reuse any current frame already at an anchor. Record
`(timestamp, content, target-title evidence)` in a sampling ledger and retain
repeats at distinct times. Do not revisit a reliably read timestamp unless its
frame was unreadable, target identity was uncertain, or later evidence conflicts.

## Refine intervals

- After four anchors, pause before any further seek. Reserve a conservative action
  budget for all remaining non-video work, and recompute it before every refinement;
  refine only if that leaves enough budget for completion.
- Do not use pure binary search for unknown contents: it assumes a monotonic
  transition. Prioritize differing or ambiguous endpoints and suspected short
  events; use midpoints to establish order or narrow a transition.
- If the task needs ordered values rather than transition timestamps, stop
  narrowing a transition once the values and order are established. Do not add
  coverage samples merely to reconfirm already supported intervals or endpoints.
- Stop when evidence is sufficient or another seek threatens task completion.
  After each seek, reacquire the frame. On title mismatch, reopen the target,
  pause, and restore the last verified timestamp.

## Completion evidence

Keep ordered content, sampled timestamps, refined transitions, and unresolved intervals.
Four anchors are a coarse pass, never proof of exhaustive coverage.
