---
name: androidworld-markor-output-directory
description: Place unspecified AndroidWorld Markor outputs in the benchmark's expected default notes directory.
version: 1.0.1
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

- Establish the output path from the current folder and entered filename.
  When these already identify `Documents/Markor`, no extra return to the list
  or reopening is needed to establish the location.
