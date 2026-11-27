# GUI 脚本与 Skill 回归操作指南

在含 GUI 脚本评测模块的开发源码树根目录运行 `python -m evaluation.gui_scripts`。`--env-file` 放在子命令前，省略时读取 `.env`。`draft`、自动选区的 `record`、`regress` 会使用配置的模型。普通 `replay` 先按脚本执行，需要正式接管时才调用配置的模型。定位、回放和版本登记的实现见 [GUI 脚本详设](../docs/gui-script-design.zh-CN.md)。

## 准备独立模拟器

在 Android Studio 的 Device Manager 中创建一个专用 AVD，例如 `ClickClickGuiTest`。使用独立于日常任务和 AndroidWorld 的 AVD 与端口。关闭连接到这个测试设备的其他任务后，在一个终端启动模拟器。

```powershell
emulator -avd ClickClickGuiTest -port 5556 -no-snapshot-load -no-snapshot-save
```

在另一个终端确认序列号、AVD 名和系统已启动。安装要测的 App 版本，准备可重建的测试数据，将 App 停在脚本或用例所需的初始界面，再保存命名快照。`sys.boot_completed` 应输出 `1`。

```powershell
adb -s emulator-5556 wait-for-device
adb -s emulator-5556 shell getprop sys.boot_completed
adb -s emulator-5556 emu avd name
adb -s emulator-5556 emu avd snapshot save clickclick-clean
```

恢复快照会回退整个 AVD。每个要测试的 App build 单独准备和记录快照，新旧 build 分开运行。以下示例中的包名、控件和 build 值是测试样例，使用时替换成实际设备观察到的值。每次运行选择新的输出路径。遇到 `device_reconciliation_required` 或上一轮待核对状态时，先确认上一轮的设备输入与任务均已结束，再处理测试设备。

## `draft` 从成功任务起草脚本

`--runtime` 指向来源任务的数据目录，里面包含 `clickclick.db` 和 `artifacts/`。`--output` 是一个新文件。起草和独立 Reviewer 通过后，文件包含 `candidate.script`、`script_hash`、来源轨迹和审查结果，状态为 `candidate`，并标记 `replay_required: true`，供显式回放。

```powershell
python -m evaluation.gui_scripts draft --runtime data/source-run --task-id TASK_ID --output data/gui/draft.json
```

起草要求成功的单 App Android 轨迹，动作须有已确认的派发以及可关联的前后观察。起草器会核对语义目标和实际动作，Reviewer 再判断脚本能否完成源目标。观察缺失、动作不受支持或无法表达独立结果时会跳过或拒绝候选。生成的脚本从 App 中可观察的初始状态开始。

## `record` 从纯截图录制

准备同一设备上点击前后的完整 PNG 和实际触点。Learner 自动选出对象标识、控件、可见边界和点击后的结果区域。程序从原图裁切并验证选区，可以用重复的 `--reference-before` 传入同一初态的额外帧来验证选区是否稳定。

```powershell
python -m evaluation.gui_scripts record --before before.png --after after.png --click 600 457 --goal 'Turn off the weekday alarm at 06:15.' --app-id com.google.android.deskclock --script-id disable-weekday --output data/gui/disable-weekday
```

录制输出 `script.json` 和 `assets/`。调用 `replay --script data/gui/disable-weekday/script.json` 时会自动读取旁边的模板。遮罩只保留选中的区域。视觉回放使用设备原始分辨率的 ADB 截图核对身份和触点。执行器在允许的范围内比较对象，只有同一对象和唯一控件都能确认才计算触点。整体移位时检查同一组区域，内部重排时从当前截图的可见分隔或背景块建立候选行和卡片。身份歧义或无法确认归属时等待，超时进入脚本异常处理。

手动指定 `--before-roi` 时，可另加 `--before-fallback-roi` 限定扩到哪个区域。需要复现某组布局时，用 `--selection` 提供 `before` 和 `after` 的选区。每侧包含 `box`、`container`（`card`、`row` 或 `panel`）和 `parts`，每个部件声明 `role` 和 `box`。裁切框使用原图像素，顺序为左、上、右、下，右、下不包含在框内。仅保存了单个模板的旧纯图脚本可以读取，自动点击前需要通过录制入口补齐对象证据。

多步骤用 `--manifest` 一次提交。文件是按顺序排列的 JSON 数组，每项包含 `before_png`、`after_png` 和 `click`，可选 `selection`、`reference_before`、`before_roi`、`before_fallback_roi` 和 `after_roi`。截图路径相对于清单目录，ROI 使用从 0 到 1 的屏幕比例。

新建任务时勾选「录制脚本」，也可以在创建任务请求中传入 `generate_test_script: true`。开启后，已接受观测里的未标注图像按观测 ID 记录在 `script_recording_frame`，原始文件保存在 `artifacts/script_recording_frames/`。记录包含实际图像尺寸、设备坐标尺寸、旋转和裁剪范围。

任务成功后，后台 Learner 会把有可靠树证据的动作和需要图像定位的动作合成一份草稿。任务详情提供状态与下载。下载包里包含 `script.json` 和脚本引用的图像素材。解压后将 `script.json` 传给 `replay`，`--goal` 填文件中 `provenance.goal` 的原文。后台生成不接管设备，草稿需要独立回放验证。自动生成适用于新版开启录制的任务。

配置 `CLICKCLICK_SKILL_LOOP_FIXTURE_JSON` 后，录制任务的草稿会自动在专用模拟器上进行两次独立回放。快照需满足脚本声明的初态。缺少参数或无法复现初态时保留草稿。程序可确认低影响的脚本会自动登记到运行目录的 `scripts/`。其余通过复验的脚本在任务详情中待审批。录制复验和 Skill 回归共享专用设备的使用顺序。

如需手写文字身份条件，视觉对象的 `identity` 部件可声明固定 `text` 或已声明的 `text_parameter`。运行 `replay` 或 `regress` 时，用 `--ocr-executable` 指定本地 Tesseract，`--ocr-language` 指定所需语言。脚本会在同一个可见容器里要求唯一、精确的文字命中。

## `replay` 验证脚本

`--script` 可传 `draft` 的完整输出，也可传独立的脚本 JSON。传审查产物时，脚本哈希和 `--goal` 必须与产物中的记录一致。独立脚本示例 `script.json` 如下，参数文件中的键必须与 `parameters` 完全一致。

```json
{
  "schema_version": 1,
  "script_id": "enter-new-note-title",
  "revision": 1,
  "app_id": "com.example.notes",
  "platform": "android",
  "parameters": {"title": {"type": "string", "max_length": 80}},
  "initial": [{"type": "target_present", "target": {"text": "Notes"}}],
  "steps": [
    {
      "id": "open-note",
      "before": [{"type": "target_present", "target": {"text": "Notes"}}],
      "action": {"type": "tap", "target": {"text": "New note", "role": "Button"}},
      "after": [{"type": "target_present", "target": {"resource_id": "note_title"}}]
    },
    {
      "id": "enter-title",
      "before": [{"type": "target_present", "target": {"resource_id": "note_title"}}],
      "action": {"type": "type", "target": {"resource_id": "note_title"}, "parameter": "title"},
      "after": [{"type": "target_present", "target": {"resource_id": "note_title", "text_parameter": "title"}}]
    }
  ],
  "final": [{"type": "target_present", "target": {"resource_id": "note_title", "text_parameter": "title"}}]
}
```

`parameters.json`

```json
{"title": "Example Renamed"}
```

`type` 要求目标已在当前界面获得输入焦点。示例从空白的新标题栏开始，最终验收是编辑器显示指定标题。如需检验保存效果，把保存作为实际步骤，并将 `final` 写成保存后能观察到的结果。选择器在每个新观察中解析。同名目标可通过 `ancestor` 限定归属，或用 `relative_to` 指定锚点关系。例如下面的选择器查找 `Team notes` 右侧距离最近的 `New note`。

```json
{"text":"New note","role":"Button","relative_to":{"anchor":{"text":"Team notes"},"relation":"right_of"}}
```

顺序本身有业务含义时，可用从零开始的 `occurrence`，并声明 `expected_count` 或限定 `ancestor`。`tap` 也可附 `visual` 模板，以便在同名控件间定位，或在整页无树时用纯图点击。候选暂时无法区分时会在本步的等待期限内重新观察。仍无法确定时进入脚本的异常处理，开启接管时可交给 ClickClick。`timeout_seconds` 可按步骤提供，`before`、`after` 和 `final` 使用结构化条件。 [Maestro 选择器](https://docs.maestro.dev/reference/selectors/relational-selectors) · [Airtest 图像脚本](https://airtest.doc.io.netease.com/en/IDEdocs/airtest_framework/3_airtest_image/)

```powershell
python -m evaluation.gui_scripts replay --script script.json --parameters parameters.json --goal 'Enter the title in a new test note.' --serial emulator-5556 --avd ClickClickGuiTest --snapshot clickclick-clean --seconds 90 --output data/gui/replay-01
```

检查 `report.json` 中的 `first.status`、`first.receipt.final_asserted` 和每步回执。提供 `--snapshot` 时，首次回放从该快照开始。换一组业务输入时使用新参数文件和独立回放输出。

步骤的 `before` 和 `after` 各有自己的条件等待。同一次后置等待共用一个截止时间。普通默认值为 12 秒，启动 App 默认值为 15.5 秒。`timeout_seconds` 可以覆盖默认值。可选 `blocked_when` 描述明确的阻塞条件，观察到它就停止等待。输入目标要与当前实际聚焦的可编辑控件一致。

### 接管、提交核查和续跑

交还时，运行器用最新观测核对当前失败步的结果，把完成证据和下一步位置一起保存。进入下一步后才检查它的前置条件。下一步也失败时，会开始一轮独立接管。普通 `replay` 可以先用已配置的有限接管，随后交给正式 Executor。当前路径失效时，由 Executor 发出 `replan`，Harness 将当前失败步、已完成步骤和设备回执一起交给 Planner。所有接管共用原任务的动作预算和接管时限，后者可用 `--takeover-seconds` 设置。独立验证模式仍关闭接管。

可能已经提交的步骤进入只读核查。T1 记录派发意图，T2 记录设备回执，T3 持久化执行记录。业务完成另以步骤证据和续跑位置原子保存。对具有稳定记录区的提交步骤，可设置 `confirmation` 指向记录容器 `scope`、记录编号节点 `target` 和文本前缀 `identifier_prefix`。运行器在提交前记录该容器内已有的编号，提交后可用其中唯一的新编号确认这次提交，即使普通确认页没有出现。记录必须在可核对的 UI 树里。如果需要额外导航才能看到记录，请操作员打开对应记录后点「恢复」。纯图定位仍可用于派发。异常提交的自动读回需要可识别的树上编号。

仍无法核实时，任务暂停并请求人工确认。通过挂载原回放运行目录的 Console 可以使用任务详情的「恢复」。命令行也可以使用原脚本版本、业务参数、设备和输出目录，把 `first.task_id` 传给 `replay --resume-task`。续跑直接核查同一次提交，不加载快照。已触发的回放也可经现有崩溃恢复入口继续核查。

### `--repair` 验证一次受控接管后的修订

需要同时提供 `--snapshot` 和 `--assist-policy`。有限接管模型在策略列出的 App 内动作中选择，运行器重新观察并验证每个动作的前后条件。纯清障动作如关闭非提交弹窗，可以在经过审查的策略中列入 `recovery_only_actions`。下面的 `assist.json` 允许通过另一个按钮打开同一个新建笔记界面。

```json
{
  "max_actions": 1,
  "max_seconds": 20,
  "actions": [
    {
      "id": "open-note-alternative",
      "before": [{"type": "target_present", "target": {"text": "Notes"}}],
      "action": {"type": "tap", "target": {"desc": "Create note", "role": "Button"}},
      "after": [{"type": "target_present", "target": {"resource_id": "note_title"}}]
    }
  ]
}
```

```powershell
python -m evaluation.gui_scripts replay --script script.json --parameters parameters.json --goal 'Enter the title in a new test note.' --serial emulator-5556 --avd ClickClickGuiTest --snapshot clickclick-clean --assist-policy assist.json --repair --seconds 180 --output data/gui/repair-01
```

首次接管且终检通过后，Learner 才尝试修订。当前只接纳可归因到一个未派发失败步骤的改动，保留脚本目标、参数、最终检查及其他步骤。已脱敏的接管输入缺少可核验参数时会跳过。产生候选后，同一命令重新加载同一快照、核对 App build 并禁用接管做第二遍验证。以 `report.json` 的 `update` 为准。`verified_pending_review` 表示这一次独立回放通过，修订还待审核，旧脚本文件继续保留。证据保存在 `first.json`、`repair.json`、`second.json` 和各轮数据目录中，按实际执行阶段生成。

录制版已登记后，使用 `replay --auto-repair-registry <registry> --snapshot <snapshot>` 可以让执行器从已登记脚本和当前界面推导有限的接管范围。支持具有相同稳定控件身份的语义定位变化，以及能够核对归属和唯一关闭控件的 App 内弹窗。原脚本步骤恢复并通过检查时只记录接管。业务修订要在异常场景和已登记原场景都关闭接管独立通过，才进入自动登记或人工审批。纯图或无法确认业务对象的异常保留原有显式接管入口。

接管后确有可复用业务路径时，同一次修订判断可保存一条 Skill 线索。程序按原录制任务核对脚本来源与目标。任务详情显式启动 Skills 自学习时，Learner 可以引用对应回放中的原始动作与观测。自动回归从实际接管路径的起点准备初态，再以独立结果核验整条路径。

### `--verify` 和 `approve` 登记脚本版本

已审查候选首次登记前，用 `replay --verify` 加 `--snapshot` 从同一快照独立执行两次。此模式不使用接管。核对 `report.json` 的 `update` 为 `verified_pending_review` 后，可显式批准这一版。`record` 输出是可直接回放的脚本包，`approve` 接收带独立审查回执的候选。

```text
python -m evaluation.gui_scripts approve --candidate data/gui/draft.json --report data/gui/verify-01/report.json --registry data/gui/registry
```

对于修订，传入相应 `--repair` 产生的 `repair.json` 和 `report.json`。登记时重查本地运行记录、目标初态和脚本哈希。修订还会核对已登记的上一版本。脚本按 `<script_id>/<revision>.json` 保存。`replay --script <registry>/<script_id>/` 使用当前最新登记版，传明确的版本文件固定使用该版。登记入口仅写入指定的脚本目录。

## `regress` 对比普通任务中的 Skill 改动

`regress` 用同一快照和准备脚本，为原 Skill 库与候选覆盖库各执行一次普通 ClickClick 任务。每个用例声明 build、待完成目标、结果、保护条件及预期分支。`cases.json` 必须包含 `source`，也可以加入 `variant`、`near_miss` 和 `related_normal`，每个 `setup.script_id` 唯一。下面是一个源用例。

```json
{
  "source": {
    "setup": {
      "script_id": "prepare-test-note",
      "app_id": "com.example.notes",
      "initial": [{"type": "target_present", "target": {"text": "Notes"}}],
      "steps": [{
        "id": "open-note",
        "before": [{"type": "target_present", "target": {"text": "Notes"}}],
        "action": {"type": "tap", "target": {"text": "Test note", "role": "Button"}},
        "after": [{"type": "target_present", "target": {"resource_id": "note_title"}}]
      }],
      "final": [{"type": "target_present", "target": {"resource_id": "note_title", "text": "Test note"}}]
    },
    "goal": "Set the test note title to Example Renamed.",
    "build": "146",
    "parameters": {},
    "result": [{"type": "target_present", "target": {"resource_id": "note_title", "text": "Example Renamed"}}],
    "guards": [{"type": "target_present", "target": {"desc": "Test fixture marker"}}],
    "branch": [{"type": "target_present", "target": {"resource_id": "note_title", "text": "Test note"}}],
    "branch_action": "replace_text",
    "branch_after": [{"type": "target_present", "target": {"resource_id": "note_title", "text": "Example Renamed"}}],
    "branch_expected": true
  }
}
```

`skill-candidate.json` 传 Skill 的完整候选合同。`target` 是相对基线库的路径，`new_text` 是完整的 `SKILL.md`，`evidence` 用实际原生轨迹引用。示例中 `version` 和正文需根据自己的基线修改。

```json
{
  "target": "apps/com.example.notes/workflows/edit-title/SKILL.md",
  "new_text": "---\nname: edit-title\ndescription: Edit a saved note title.\nversion: '1.0.1'\nkind: workflow\napp: com.example.notes\ncapability: edit_title\n---\n\n## Procedure\n\nWith the note title editor in focus, enter the requested title.\n\n## Verification\n\nCheck that the editor shows the requested title.\n",
  "gist": "Reuse the focused title editor",
  "evidence": ["event:12@1"],
  "scope": "A test note open in the title editor",
  "benefit": "Avoid reopening an editor that is already active",
  "risks": "The title editor must be visibly focused"
}
```

```powershell
python -m evaluation.gui_scripts regress --cases cases.json --candidate skill-candidate.json --baseline-skills skills --model MODEL_ID --serial emulator-5556 --avd ClickClickGuiTest --snapshot clickclick-clean --rounds 20 --seconds 180 --fixture-seconds 90 --output data/gui/regress-01
```

`--rounds` 和 `--seconds` 是每轮普通任务的动作数和时限。`--fixture-seconds` 分别约束准备、独立验收和清理。查看 `prepared-validation.json` 的 `prepared_gate`、独立结果、分支、保护条件和成本。分支需要在同一个成功派发动作的前后观察中分别满足 `branch` 和 `branch_after`，且动作类型符合 `branch_action`。初始状态已满足结果、环境不匹配和无法确认的回执会影响该轮能否评分。`operation: "delete"` 会返回 `unsupported_operation`。

脚本起草、回放与 Skill 回归都是显式评测入口。输出保留在指定目录供审核，正常任务继续通过原有 ClickClick 入口运行。

## `skill-loop` 从任务学习并回归同一份 Skill 候选

任务详情也可以启动同一条链路。先在服务端用 `CLICKCLICK_SKILL_LOOP_FIXTURE_JSON` 配置专用模拟器，填写 `serial`、`avd`、`snapshot` 和独立的本机 `adb_port`，可选 `model`。然后在任务详情的「Skills 自学习」中确认预算和操作测试设备的授权。后台先学习和审查，再从原任务及已核验的接管证据找对应分支。经验证的已有用例会优先复用，缺用例时由 Learner 起草初态和验收计划。配对回归同时覆盖能在该测试环境核验的已登记用例。明确低影响且满足精确发布校验的改动自动发布，高影响进入待审批。控制台用例和运行证据保存在运行目录的 `skill-loop/` 下。

`--runtime` 和 `--task-id` 指向已经结束的来源任务。`--plan` 给出明确的用例和独立检查，`--registry` 保存经验证的用例。每个用例按 Skill 目标、App 包名、build 和 `case_key` 查找，命中时直接复用。也可以为用例提供相对计划文件的 `existing` 用例 JSON 路径和可选 `assets` 路径。未登记的已有用例会先独立回放。缺少现成用例时，`recording` 让 Learner 录制只准备初态的任务，回放并检查正式目标尚未完成后登记。

```json
{
  "cases": {
    "source": {
      "case_key": "prepare-timer-two-minutes",
      "app_id": "com.example.timer",
      "build": "10",
      "recording": {
        "app_id": "com.example.timer",
        "build": "10",
        "setup_goal": "Open the timer editor and leave an unstarted two-second draft.",
        "goal": "Change the timer draft to two minutes and leave it unstarted.",
        "result": [{"type": "target_present", "target": {"text": "02:00"}}],
        "guards": [{"type": "target_present", "target": {"text": "Start", "role": "Button"}}],
        "branch": [{"type": "target_present", "target": {"text": "00:02"}}],
        "branch_action": "tap",
        "branch_after": [{"type": "target_present", "target": {"text": "02:00"}}],
        "branch_expected": true
      }
    }
  }
}
```

```powershell
python -m evaluation.gui_scripts skill-loop --runtime data/source-run --task-id TASK_ID --plan plan.json --registry data/skill-cases --baseline-skills skills --model MODEL_ID --serial emulator-5556 --avd ClickClickGuiTest --snapshot clickclick-clean --rounds 20 --seconds 180 --fixture-seconds 90 --output data/gui/skill-loop-01
```

可加 `skill_target` 选定候选的相对 Skill 路径。检查 `skill-loop.json` 中哪些用例为 `reused`、`existing` 或 `recorded`，再查看 `regression/prepared-validation.json` 的实际效果、分支和保护条件。配对双方使用同一个登记初态，由普通 ClickClick 完成正式目标。评测还会从实际请求记录核对 Skill 的版本和内容。配对和 Skill 资格检查均通过后，精确候选进入待审批。
