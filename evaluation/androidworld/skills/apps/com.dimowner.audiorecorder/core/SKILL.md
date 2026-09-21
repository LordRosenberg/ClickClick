---
name: androidworld-audio-recorder-naming
description: AndroidWorld Audio Recorder named-recording input convention; evaluation profile only.
version: 1.0.0
app_aliases: [Audio Recorder]
role_sections: true
app: com.dimowner.audiorecorder
kind: app_core
capability: record_audio_with_name
source: authored
---

# AndroidWorld recording-name convention

## Shared

### Hints

- Only in the AndroidWorld evaluation profile, when creating an audio recording
  with a specified name, interpret that name as the complete value to enter in
  Audio Recorder's save-dialog name field. Carry the literal unchanged through
  stage goals, text entry and completion checks, including capitalization,
  spaces and file extensions.
- Enter the supplied name verbatim even when M4a is selected or the app appends
  an extension automatically. Do not strip an existing suffix, enter only the
  basename, or manually add another suffix to the supplied value.
- Confirm the name-field value matches the supplied literal before saving, then
  verify that the new recording was saved. Distinguish the entered recording name
  from the disk filename: the app may append its own extension. Do not rename the
  recording to remove that app-added suffix solely to normalize the disk filename.

This is a benchmark input convention, not a general rule for recording filenames.
Do not apply it to ordinary user tasks, unnamed recordings or other fields. An
explicit instruction specifying different name-field contents takes precedence.
This skill supplies no instance names, private fixtures or success verdict; a
matching input field alone does not establish that recording and saving succeeded.
