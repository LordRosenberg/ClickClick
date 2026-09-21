# Android Accessibility Collector 配置

[English](accessibility-collector-setup.md) · [中文首页](../README.zh-CN.md) · [部署指南](deployment.zh-CN.md)

ClickClick 使用轻量、只读的 Android AccessibilityService Collector 采集当前多窗口树。动作仍通过 ADB 执行，画面仍来自 scrcpy／ADB 截图；它不是通用设备 Portal。

自 Collector 0.3.0 起，服务只在任务执行期间持有可续期亮屏租约。主机每 30 秒续期一次 90 秒 TTL，任务清理时释放。主机崩溃或 ADB 断开时，Collector 的 TTL 看门狗及 Android WakeLock 自身超时都会释放租约。ADB 连续连接数天本身不会让屏幕一直亮着。

Collector 自动安装和升级。Control API 启动后立即在后台检查所有在线空闲设备，此后每 30 秒处理新上线和此前失败的设备。同一在线期间，成功设备不重复初始化；断开重连后会重新进入检查。任务启动也会在设备锁内强制检查版本，再获取亮屏租约。后台循环不会升级忙碌设备。

未显式配置 `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH` 时，优先使用 `output-metadata.json` 标为 0.4.4 的本地 Gradle `app-debug.apk`，其次下载固定版本 Release 到 `data/device-apks` 并验证 SHA-256。同版本重编译也会比较设备 APK 摘要，不同则重新安装。私有 Release 需要在进程环境配置 `GH_TOKEN` 或 `GITHUB_TOKEN`，见 [APK 分发规则](../android/prebuilt/README.md)。

## 前置条件

正常流程使用自动初始化：

1. 真机启用**开发者选项**和 **USB 调试**，或启动模拟器。
2. 通过 USB 或 `adb connect <ip:port>` 连接并授权设备，`adb devices` 应显示 `device`。
3. 按[部署指南](deployment.zh-CN.md)启动 Control API。Collector **0.4.4** 来自本地构建或 Release，无需手动安装 APK。支持 Android 8.0+／API 26。
4. 保持设备解锁，出现安装／权限提示时确认。系统要求时，在 Android 无障碍中启用 Collector，或允许受限设置。

缺少输入法时，提供 [ADBKeyboard APK 路径](deployment.zh-CN.md#input-method-apk)；API 安装配置的文件，但不自动下载。

可选 Bash bootstrap 脚本会下载 ADBKeyboard 并初始化设备。它复用本地构建优先的 Release 解析器：

```bash
bash scripts/bootstrap-device.sh YOUR_ADB_SERIAL
```

本地开发可用 Android Studio 或 Gradle 构建 `android/accessibility-collector/`，配置 `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH`，自动初始化便会使用该文件。`POST /api/devices/{serial}/initialize` 是可选诊断／重试接口，参见[初始化结果](deployment.zh-CN.md#initialization-results)。

Collector **默认启用**，`shared/config.py` 中 `CLICKCLICK_ACCESSIBILITY_COLLECTOR_ENABLED=true`，初始化后不需额外开关。

## 环境变量

```bash
export CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH=android/accessibility-collector/app/build/outputs/apk/debug/app-debug.apk
export CLICKCLICK_IME_APK_PATH=data/device-apks/ADBKeyboard.apk
export CLICKCLICK_DRIVER_URL=
```

初始化是幂等的：安装／升级 APK，在保留其他无障碍服务的前提下合并启用 Collector，准备测试输入法而不永久选用，并按需设置插电常亮。每一步报告 `ready`、`degraded`、`operator_action_required` 或 `failed`。

ADB 授权、Android／厂商受限设置确认和安全锁屏仍需操作人员处理。Collector 不可用时，Driver 使用新的 `uiautomator dump`，标记不完整并自动附带截图。设置 `CLICKCLICK_ACCESSIBILITY_COLLECTOR_ENABLED=false` 可回退到只使用 dump 的结构采集。

## 验收基准

初始化后运行仓库基准：

```bash
python scripts/measure_observation_latency.py --serial SERIAL --repetitions 20
```

分别记录简单页面、大树、弹窗、可见 IME、覆盖层、视频／自定义表面、Collector 禁用和新 dump 回退场景的 p50／p95。采集元数据包括 Collector 获取、逐窗口遍历、归一化、截图／提供方阶段、节点数、完整性和降级路径。

当前 Collector 首次尝试超时为 2500ms（`driver/observation_deadline.py` 中 `TREE_PRIMARY_ATTEMPT_TIMEOUT_MS`），依据 API-35 小米设备测量校准：ADB ContentProvider 往返 p95 约 1.1–1.34 秒，窗口遍历低于 62ms，ADB screencap p95 低于 0.36 秒；1500ms 试验曾造成误降级长尾。`4000` 节点和 `60` 深度限制是保守安全上限，只应依据测量调整，不按应用或像素阈值调整。

## 0.4.4：动态内容和窗口切换

每次请求只遍历一遍树。并发快照请求立即返回 `snapshot_busy`，不会排队或在另一遍采集期间清空缓存。设备端在节点遍历间检查 2200 ms 预算，主机仍保留 2500 ms 树预算；设备端检查无法打断正在阻塞的 Android Binder 调用。

0.4.4 在 API 33+ 对活动窗口根节点和子节点使用包含祖先、兄弟的深度优先批量预取，并设置 `FLAG_PREFETCH_UNINTERRUPTIBLE`；Android 每批最多返回 50 个节点。活动根节点的窗口 ID 必须与窗口列表匹配，否则回到该窗口自身的取根接口。每轮仍清空缓存，不复用上一轮树、不延长超时预算；API 26–32 保留原有读取接口。

普通内容更新只保留为诊断信息。0.4.4 仅过滤具有有效窗口 ID 的纯窗口标题/无障碍焦点事件，以及纯面板标题事件；混合标志、零值或未知标志、缺少窗口 ID、面板出现/消失、输入焦点、活动窗口、几何或结构变化仍递增窗口代数。API 26/27 无法读取窗口变化子类型，保留保守处理。健康接口以 `filtered_window_events` 记录过滤数量。主机在采集前后校验窗口代数，并要求最近 300 ms 无需要失效处理的窗口事件。发现切换时使用事务已有的一次延迟重采，不直接把这一帧降级成可点击的单图；第二次仍不安全则显式报采集失败。窗口校验通过时，缺树仍允许单图降级。

这不保证同一窗口内 HTML 加载或动画已经结束，也不判断页面语义。需要新证据时由模型调用 `observe_screen`。窗口静默时间、遍历次数、取根和取子节点耗时保留在诊断中。

## 启动 Driver

本地设备使用 Control API 内嵌 Driver。只有远程设备主机需要独立 Driver，然后在 API 主机将 `CLICKCLICK_DRIVER_URL` 配置为该主机 RPC URL：

```bash
clickclick-driver
# or: python -m driver.main
```

## 实时镜像

Console Live 使用仓库固定的独立 **scrcpy-server** jar（`driver/vendor/scrcpy-server-v3.3.1.jar`，版本固定在 `driver/scrcpy_mirror.py`），开启 `raw_stream=true`：

- **本地设备**：Control API 在 API 主机推送／转发 jar，将 H.264 传到 `/api/device/mirror/stream`。
- **远程 Hub**：`clickclick-driver` 提供 `/mirror/stream`，API 向浏览器转发字节；实验室主机必须携带相同 jar。
- **浏览器**：使用 Chromium WebCodecs（`VideoDecoder`）。Vite 开发代理需要 `ws: true`，仓库已配置。

Agent 画面仍使用 scrcpy／ADB，**不依赖** Console Live。语义树优先使用 Collector，仅在降级时使用新的 `uiautomator dump`。升级步骤见 [driver/vendor/README.md（英文）](../driver/vendor/README.md)。
