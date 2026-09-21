# 部署与运行

[English](deployment.md) · [中文首页](../README.zh-CN.md) · [架构](architecture.zh-CN.md) · [评测环境](evaluation.zh-CN.md)

## 环境要求

- Python 3.12 是固定视频解码器已验证的选择。项目元数据声明 Python 3.11+，解码器 wheel 兼容性是另一项约束。
- Node.js／npm 用于构建 Vite Console，使用已提交的锁文件执行 `npm ci`。
- Collector 最低支持 Android 8.0+，目标设备需授权 ADB。AndroidWorld 运行器有更严格的 API 33／模拟器要求。
- 真实 Agent 任务需要可用模型。Fixture 模式替换设备 I/O，不替换语言模型。

按 [README](../README.zh-CN.md#部署) 中对应平台命令创建并激活 `.venv`，安装 `python -m pip install -e ".[decode]"`；运行测试时加入 `dev` 扩展。已有 `.env` 应保留，不用示例覆盖。

<a id="model-routing"></a>
## 模型路由

`CLICKCLICK_DEFAULT_MODEL` 必须匹配 `CLICKCLICK_MODELS_JSON` 中的键。键包含路由前缀，值描述提供方。真实凭据只在本地填写，不写入跟踪文件。示例文件是模板，不是可直接使用的模型服务。

兼容 OpenAI 的接口配置如下，替换所有占位值：

```dotenv
CLICKCLICK_DEFAULT_MODEL=openai/YOUR_MODEL_ID
CLICKCLICK_MODELS_JSON={"openai/YOUR_MODEL_ID":{"provider":"openai","base_url":"https://YOUR_ENDPOINT/v1","api_key":"YOUR_API_KEY","max_tokens":4096,"reasoning_supported":false,"stream":false}}
```

可选 `CLICKCLICK_MANAGER_MODEL` 指定 Planner／Reviewer 共用模型，`CLICKCLICK_EXECUTOR_MODEL` 指定 Executor，均须对应已配置条目。角色覆盖为空时使用默认模型。推理能力与参数按模型／提供方显式配置，不从名称推断。

每个模型条目可在顶层设置 `"tool_choice":"auto"` 或 `"tool_choice":"required"`，与 `max_tokens` 同级，不放入 `extra_body`。省略时保持 `required`；修改一个模型不会改变其他模型（包括 GPT）的策略。`auto` 允许模型先回复文字，但 Agent 仍须收到有效的决策提交工具调用才能结束该轮。此配置本身不启用 thinking，也不提供部分思考模型所要求的 `reasoning_content` 多轮回传支持。

对于 LiteLLM 模型注册表未识别的兼容中转别名，可在该模型条目顶层显式设置 `"allowed_openai_params":["reasoning_effort"]`，配合 `"reasoning":{"effort":"high"}` 使用。这会保留推理强度并通过 LiteLLM 参数校验；上游接口仍须支持该参数。此覆盖只作用于该模型，不开启全局参数丢弃。

本地已验证研究使用以下订阅路由：

```dotenv
CLICKCLICK_DEFAULT_MODEL=chatgpt/gpt-5.6-sol
CLICKCLICK_MODELS_JSON={"chatgpt/gpt-5.6-sol":{"provider":"chatgpt","reasoning_supported":true,"reasoning":{"effort":"high","summary":"concise"},"stream":true}}
```

运行任务前使用项目登录工具认证：

```bash
python -m shared.chatgpt_login
```

此路由使用登录 token，而非上述 API key／base URL 字段。`CLICKCLICK_CHATGPT_TOKEN_DIR` 可指定 token 存储位置。提供方／账户可用性可能不同，实验记录不保证每个账户都能使用该模型。项目配置见 [.env.example](../.env.example) 和[模型路由](../shared/model_router.py)。

## 本地设备部署

Agent Harness 默认 `CLICKCLICK_AGENT_ARCHITECTURE=plan_executor`，即两角色加显式按需审核。`plan_reviewer` 要求最终审核；旧 `contract` 模式已删除。除非刻意比较策略，否则保留默认值。上下文调优属于 Harness，详见[设计取舍](design-decisions.zh-CN.md#context-budget)。

设备连接到 API 主机时：

```dotenv
CLICKCLICK_DRIVER_URL=
CLICKCLICK_USE_FIXTURE_DRIVER=false
```

若此前使用远程 Hub，清除 `CLICKCLICK_DRIVER_URLS_JSON` 中的对应条目。复制的示例包含远程 Driver URL，单进程部署必须显式清空。

```bash
adb devices
npm --prefix web ci
npm --prefix web run build
clickclick-api
```

打开 `http://127.0.0.1:8080`。平台自动检查已连接的空闲设备，安装／升级 Collector、启用无障碍、检查通道并准备配置的输入法。正常启动不需要手动安装 APK 或调用初始化 API。

保持设备解锁。设备出现安装、USB 调试或权限提示时确认；已授权 ADB 的安装并非每台设备都需要屏幕确认。若 Android 限制 Collector，在无障碍设置中启用它，并在应用详情存在该选项时允许受限设置。未解决的问题参见[初始化诊断](#initialization-results)。

Collector 依次选择 `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH`、版本匹配的本地 Gradle 产物、固定版本且校验 SHA-256 的 GitHub Release。同版本本地重编译按已安装 APK 摘要识别。Release 缓存位于 `data/device-apks`，可用 `CLICKCLICK_DEVICE_APK_CACHE` 覆盖；私有 Release 需要在进程环境设置 `GH_TOKEN` 或 `GITHUB_TOKEN`，仓库可用 `CLICKCLICK_COLLECTOR_RELEASE_REPO` 指定。设备未安装 ADBKeyboard 时，`CLICKCLICK_IME_APK_PATH` 必须指向已有 APK，API 不会自动下载。相对路径在运行进程的主机上解析；远程设备需要在 Driver 主机提供文件和配置。

初始化启用 ADBKeyboard，但不将其选为当前输入法。任务就绪检查会在动作前选择并验证 IME，因此应检查 `test_ime.status`，不能只检查 Collector。

<a id="input-method-apk"></a>
### 输入法 APK

已安装 ADBKeyboard 或配置了有效 APK 路径时跳过本步。新设备只需提供一次来源文件，平台负责安装。可从 [ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard) 获取 APK。下方跨平台命令下载项目 bootstrap 脚本使用的同一文件，不会在设备上安装：

```bash
python -c "from pathlib import Path; import urllib.request; p=Path('data/device-apks/ADBKeyboard.apk'); p.parent.mkdir(parents=True, exist_ok=True); urllib.request.urlretrieve('https://raw.githubusercontent.com/senzhk/ADBKeyBoard/master/ADBKeyboard.apk', p)"
```

启动前在 `.env` 设置路径；服务已运行时，修改后重启：

```dotenv
CLICKCLICK_IME_APK_PATH=data/device-apks/ADBKeyboard.apk
```

下载不可用时，从上游项目获取文件并配置本地路径。输入法文件缺失会报告初始化失败／降级，不会静默下载。Collector 文件缺失时可按 [Collector 指南](accessibility-collector-setup.zh-CN.md)构建。

### 可选 bootstrap 工具

Bash 环境可使用 bootstrap 脚本。它默认引用的 Collector 版本早于当前运行时，需显式传入当前 APK：

```bash
bash scripts/bootstrap-device.sh YOUR_ADB_SERIAL
```

脚本在需要时下载输入法 APK，签名不匹配时可能替换辅助应用。API 初始化器在签名阻止升级时也只替换无状态的 Collector 包，不清空目标应用数据。手动／构建说明见 [Collector 指南](accessibility-collector-setup.zh-CN.md)。

<a id="initialization-results"></a>
### 初始化结果

此接口用于诊断或修复问题后重试，正常启动时可不调用。保持服务运行，将 `emulator-5554` 替换为目标序列号：

```bash
# Linux / macOS
curl -X POST http://127.0.0.1:8080/api/devices/emulator-5554/initialize
```

```powershell
# Windows PowerShell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8080/api/devices/emulator-5554/initialize' | ConvertTo-Json -Depth 8
```

响应包含顶层 `status` 和 `steps` 对象，逐项检查：

| 步骤 | ready 的含义 | 未就绪时的处理 |
| --- | --- | --- |
| `adb` | 目标在线且已授权。 | 检查序列号、线缆／模拟器、授权提示与 ADB 主机。 |
| `collector_apk` | 已安装所需 Collector。 | 提供匹配 APK，检查安装／版本错误。 |
| `accessibility_service` | Collector 已启用，所需设置已接受。 | 按 `guidance` 进入 Android 设置 → 无障碍 → ClickClick Accessibility Collector；有受限设置选项时在应用详情允许。 |
| `collector_health` / `collector_channel` | 服务响应且通道可预热。 | 重新检查无障碍、解锁设备，再初始化。 |
| `test_ime` | 配置的输入法已安装并启用。 | 提供[输入法 APK](#input-method-apk)，修正 `CLICKCLICK_IME_APK_PATH`；修改 `.env` 后重启，等待自动重试或调用上述接口。 |

期望总状态为 `ready`。`degraded` 表示部分环境可用，`operator_action_required` 需要设备端操作，`failed` 需修正所报故障。HTTP 409 表示设备忙，应结束或取消任务后再初始化。这些状态检查设备组件，不验证模型凭据或 scrcpy 是否成功出帧；首次任务还会检查这些通路。

手动验证注册信息时替换序列号：

```bash
adb -s emulator-5554 shell ime list -s
adb -s emulator-5554 shell settings get secure enabled_accessibility_services
```

IME 列表应有 `com.android.adbkeyboard/.AdbIME`，已启用无障碍服务中应有 `ai.clickclick.collector/.CollectorService`。不要整体覆盖无障碍服务设置，以免移除其他服务。测试后可按需在 Android 设置中恢复常用键盘。

Console 与 Agent 按需使用仓库固定版本的 scrcpy server，无需独立启动桌面 scrcpy。ADB 画面回退会被记录；缺少视频解码能力会使严格评测预检不通过。

## Windows ADB 与调试

解析器检查 `PATH`、`CLICKCLICK_ADB_PATH`、Android SDK 位置和项目缓存。Windows 上发现失败时可准备经过校验和验证的 Google Platform-Tools。`CLICKCLICK_ADB_AUTO_DOWNLOAD=0` 禁用该回退；安装多个 SDK 时，显式路径可避免歧义。

[VS Code 启动配置](../.vscode/launch.json)默认使用 `.venv/bin/python`，Windows 使用 `.venv/Scripts/python.exe`。无需设备的 UI 调试可使用 fixture API 配置；真实 Agent 任务仍需所选模型可用。

## Fixture 与远程模式

本地 fixture：

```dotenv
CLICKCLICK_USE_FIXTURE_DRIVER=true
CLICKCLICK_DRIVER_URL=
```

正常启动 `clickclick-api`。无需提供方调用的确定性测试应使用项目假 Agent 测试，而非提交真实模型任务。

远程设备主机需安装兼容代码和解码支持，授权其连接的设备，然后启动：

```bash
clickclick-driver --host 0.0.0.0 --port 8765
```

API 主机配置：

```dotenv
CLICKCLICK_USE_FIXTURE_DRIVER=false
CLICKCLICK_DRIVER_URL=http://DEVICE_HOST:8765
```

多个 Hub 可通过 `CLICKCLICK_DRIVER_URLS_JSON` 配置，例如 `[{"id":"lab-a","url":"http://DEVICE_HOST:8765"}]`，此时设备键包含 Hub。平台与 Hub 的代码／协议版本需兼容，包括固定 scrcpy server。设备控制接口应部署在可信网络。

## HTTPS 与前端开发

构建后的 Console 由 Control API 提供，独立 Vite 开发服务器是可选的：

```bash
npm --prefix web run dev
```

Vite 使用 5173 端口，将 `/api` 代理到 Control API。Driver RPC 默认 8765，Control API 默认 8080。不需要 Console 时，可用 `clickclick-agent "YOUR_TASK"` 运行独立任务。

通过局域网地址查看 Live 时启用 HTTPS，浏览器 WebCodecs 需要安全上下文。配置两个证书路径：

```dotenv
CLICKCLICK_API_HOST=0.0.0.0
CLICKCLICK_API_SSL_CERTFILE=./data/certs/cert.pem
CLICKCLICK_API_SSL_KEYFILE=./data/certs/key.pem
```

API 按需生成本地开发证书，并将该公共端口上的普通 HTTP 重定向到 HTTPS。这是开发证书流程，不是生产证书管理。Localhost 可使用普通 HTTP。

## 运行检查

| 现象 | 检查项 |
| --- | --- |
| Console 无设备 | ADB 授权、预期本地／远程 URL、Hub 可达性和设备初始化。 |
| 模型在动作前失败 | 模型 ID 与配置目录匹配、对应路由登录／凭据、记录的网关错误。 |
| 页面加载但 Live 空白 | 安全上下文、服务端版本、设备流诊断和解码器安装。 |
| 前端不存在 | 执行构建；API 可在缺少 `web/dist` 时启动，但不会代为构建 Console。 |
| AndroidWorld 找不到 NEXT | 引导阶段使用所需无障碍观察器，保存快照前验证应用就绪。 |

`data/` 包含本地数据库、截图、轨迹及可能存在的登录材料，不属于分发文档。纳入版本管理的配置和实验产物应标识版本与设置，不包含凭据。
