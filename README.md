<div align="center">

# ClickClick

**Give your AI the ability to get things done on Android.**

Cross-app tasks · Reliable device interaction · Reusable skills · Full execution visibility

[中文](README.zh-CN.md) · [Demos](#see-it-in-action) · [Quick Start](#deployment) · [Architecture](docs/architecture.md) · [Docs](#documentation)

[![AndroidWorld](https://img.shields.io/badge/AndroidWorld-99.14%25%20%28115%2F116%29-14866d)](docs/androidworld-results-20260921.md)
[![Python](https://img.shields.io/badge/Python-%3E%3D3.11-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/License-Apache--2.0-green)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Android%208.0%2B-3DDC84?logo=android&logoColor=white)](docs/deployment.md)

</div>

ClickClick is an open-source Android agent platform. Give it a goal in natural language: transfer information between apps, fill complex forms, manage collections or configure a schedule. It plans the work, operates the device and checks the result, with a web Console that lets you follow every step.

## 99.14% on AndroidWorld

**115 of 116 tasks passed**, covering multi-step tasks across Android apps. Evaluated with `chatgpt/gpt-5.6-sol` (high) on Android API 33 using AndroidWorld's official success checks. [Evaluation report →](docs/androidworld-results-20260921.md)

## See it in action

Real recordings of successful tasks. Click a preview to open the complete video at **3× speed**.

| Notes → recipes | Image → expense records | Recurring calendar event |
| --- | --- | --- |
| [![Transfer recipes from Markor to Broccoli](docs/assets/demos/notes-to-recipes.gif)](docs/assets/demos/notes-to-recipes.mp4) | [![Read expenses from an image and enter them in Pro Expense](docs/assets/demos/image-to-expenses.gif)](docs/assets/demos/image-to-expenses.mp4) | [![Create a recurring calendar event](docs/assets/demos/recurring-calendar-event.gif)](docs/assets/demos/recurring-calendar-event.mp4) |
| Read a note and create **three recipes**, preserving ingredients, directions and other fields. | Read a source image and enter **three expenses** with their amounts, categories and notes. | Set the date, start time, **45-minute duration**, description and daily recurrence. |

| Deduplicate expenses | Create an ordered playlist | Summarize weekly activity |
| --- | --- | --- |
| [![Remove exact duplicate expenses while preserving distinct records](docs/assets/demos/deduplicate-expenses.gif)](docs/assets/demos/deduplicate-expenses.mp4) | [![Find songs and create a playlist in the requested order](docs/assets/demos/ordered-playlist.gif)](docs/assets/demos/ordered-playlist.mp4) | [![Find this week's swimming activities and total their duration](docs/assets/demos/weekly-activity-summary.gif)](docs/assets/demos/weekly-activity-summary.mp4) |
| Inspect a long expense list, remove **exact duplicates** and retain one copy of every unique expense. | Create a named playlist, find **two songs** and verify their requested order. | Find **this week's swimming activities**, inspect durations and report the total in minutes. |

[Task instructions and recording details](docs/demos.md)

## Built for complex tasks

- **Plan, act and adapt.** Break a goal into meaningful stages, change course when the screen reveals new information, and continue from completed work. Independent review is available when a task needs another judgment.
- **Carry information across apps.** Keep source facts, progress and task notes throughout long tasks. Retrieve earlier screenshots and records when details need checking, even after context compression.
- **Interact reliably with real interfaces.** Combine screenshots and accessibility structure to locate controls. Bind supported clicks to observed native nodes, verify text entry, and feed action results back into the next decision.
- **Teach reusable app skills.** Add application knowledge without retraining the model. Skills guide planning, execution and verification; supported compound actions can inspect a detail page and return while preserving what was read.
- **Choose your model and device.** Configure models for planning and execution, connect local phones or emulators, or host devices behind a remote Driver.

The agent harness brings these capabilities together: **revisable plans + persistent memory + scoped skills + device feedback**. [Technical overview](docs/reliability-design.md)

## See and understand every run

The Console puts the live device and the agent's execution history in one workspace.

- **Watch progress:** follow the live screen, current stage and streaming model responses.
- **Inspect decisions:** open a Timeline call to see the actual model input, returned decision, active skills, tool calls and associated screenshot.
- **Diagnose failures:** trace an action to its target, receipt and resulting observation; revisit earlier screens without losing the current device view.
- **Understand resource use:** inspect task and model latency, call counts and reported input, output and cache usage.

[Console and observability guide →](docs/observability.md)

## Architecture

![ClickClick architecture](docs/assets/clickclick-architecture.svg)

**Planner → Executor → observation → continue or replan**, with Reviewer available on demand. The harness manages context, skills and execution; a persistent Session retains task evidence; Android tools supply actions and observations. Console exposes the run, and external evaluators check task outcomes.

[Architecture and module interfaces](docs/architecture.md) · [Design decisions](docs/design-decisions.md)

## Deployment

Use Python 3.12, Node.js/npm, Android SDK Platform-Tools and a model service supporting images and tool calls. Start with a locally connected device; see [deployment](docs/deployment.md) for remote hosts.

### 1. Install and configure a model

Run from the repository root:

```bash
# Linux / macOS
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[decode]"
cp .env.example .env
```

```powershell
# Windows PowerShell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[decode]"
Copy-Item .env.example .env
```

Set your model and credentials in `.env`. For an OpenAI-compatible API:

```dotenv
CLICKCLICK_DEFAULT_MODEL=openai/YOUR_MODEL_ID
CLICKCLICK_MODELS_JSON={"openai/YOUR_MODEL_ID":{"provider":"openai","base_url":"https://YOUR_ENDPOINT/v1","api_key":"YOUR_API_KEY","max_tokens":4096,"reasoning_supported":false}}
CLICKCLICK_DRIVER_URL=
CLICKCLICK_DRIVER_URLS_JSON=
CLICKCLICK_USE_FIXTURE_DRIVER=false
```

See [model configuration](docs/deployment.md#model-routing) for provider options, login-based access and reasoning settings.

### 2. Connect a device

For a phone, enable USB debugging, connect it and accept the authorization prompt. For an emulator, start and unlock it. Run `adb devices`: the intended serial must have status `device`. Resolve `unauthorized` on the phone.

### 3. Start the platform and allow automatic initialization

```bash
npm --prefix web ci
npm --prefix web run build
clickclick-api
```

ClickClick detects connected devices and provisions its Accessibility Collector. Keep the device unlocked and accept Android installation, debugging or accessibility prompts when requested. For text entry, configure the ADBKeyboard APK if it is not already installed; see [input-method setup](docs/deployment.md#input-method-apk).

[Deployment and troubleshooting](docs/deployment.md) covers remote device hosts, Collector installation and model configuration.

### 4. Run your first task

Open [Console](http://127.0.0.1:8080). In the task creation form, choose a decision model, an executor model and one idle device. Enter:

> Open Android Settings, enter Wi-Fi in its search field, inspect the wireless-network settings and report their current state. Do not change any switches.

Click **submit** and follow the task in Console. Select a Timeline call to inspect the model input, tools and screenshot. Switch between **Live** for the current screen and **Frame** for the selected step.

## Describe a task in your own words

Tell ClickClick what you want done in Console—for example, “Add the recipes in this Markor note to Broccoli.” The agent plans the steps and operates the apps based on your goal and the current screen. No fixed prompt format or predefined sequence of steps is required.

[Task examples](docs/task-examples.md) illustrate requests you can make. Adapt their apps, wording and data to your needs. Adding app-specific operating knowledge through [skills](skills/README.md) is an optional extension.

### When should you add a Skill?

Start with a natural-language request; the agent uses applicable skills already included in the repository. Add or improve a Skill when:

- **An app has non-obvious behavior:** unusual save controls, hidden entry points, ambiguous controls or device-specific interactions.
- **Similar tasks repeatedly hit the same problem:** missing fields during transfer, skipped list entries, confused duplicate names or edits that were never saved.
- **You want to reuse proven operating and verification knowledge:** teach later tasks how to perform an operation and check its outcome.

Put the current goal and data in the task request, and reusable app knowledge in a Skill. Skills can reduce known errors but do not guarantee success. Inspect the delivered skills and results in Console. [How to write and validate a Skill →](skills/README.md#when-to-add-a-skill)

## Evaluation and reproduction

For AndroidWorld, the [evaluation report](docs/androidworld-results-20260921.md) specifies the model, environment, task selection and action accounting behind 115/116. The [evaluation guide](docs/evaluation.md) explains initialization, scoring and reproducibility; the [AndroidWorld adapter](docs/androidworld-benchmark.md) describes how to connect the agent to the benchmark.

## Documentation

| Guide | What you will learn | 中文 |
| --- | --- | --- |
| [Technical overview](docs/reliability-design.md) | How planning, memory, skills and device feedback work together. | [中文](docs/reliability-design.zh-CN.md) |
| [Architecture](docs/architecture.md) | Runtime roles, module boundaries and code entry points. | [中文](docs/architecture.zh-CN.md) |
| [Design decisions](docs/design-decisions.md) | Trade-offs in review, context, capture and input. | [中文](docs/design-decisions.zh-CN.md) |
| [Deployment](docs/deployment.md) | Models, devices, installation and remote operation. | [中文](docs/deployment.zh-CN.md) |
| [Observability](docs/observability.md) | Console navigation, traces and performance analysis. | [中文](docs/observability.zh-CN.md) |
| [Task examples](docs/task-examples.md) | Natural-language task examples and result verification. | [中文](docs/task-examples.zh-CN.md) |
| [Evaluation](docs/evaluation.md) | Benchmark setup, scoring and reproduction. | [中文](docs/evaluation.zh-CN.md) |
| [Skills](skills/README.md) | App knowledge, operating guidance and skill authoring. | [中文](skills/README.zh-CN.md) |

Licensed under [Apache 2.0](LICENSE).
