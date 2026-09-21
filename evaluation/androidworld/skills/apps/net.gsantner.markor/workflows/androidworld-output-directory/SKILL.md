---
name: androidworld-markor-output-directory
description: Place unspecified AndroidWorld Markor outputs in the benchmark's expected default notes directory.
version: 1.0.0
app: net.gsantner.markor
kind: workflow
capability: androidworld_output_path
device_profiles: [androidworld_api33]
source: authored
---

# AndroidWorld Markor output directory

## Procedure

- When an AndroidWorld task requests a file "in Markor" without an output path,
  create it in `Documents/Markor`; an input in `Download` does not imply output
  there.
- At Markor's initial file list, title `Markor` with
  `/storage/emulated/0/Documents` under `..` means that list is already
  `Documents/Markor`. Create the file there; do not create another `Markor`
  subfolder.
- Follow an explicit output path when the task provides one.

## Verification

- For an unspecified "in Markor" output, verify the file in
  `Documents/Markor`, which is the directory evaluated by AndroidWorld.
