# 桌面版发布与更新维护

## 发布流程

先把桌面构建工作流和相关代码合并到默认分支，再对要发布的提交创建并推送 `desktop-v<SemVer>` 标签，例如 `desktop-v0.2.0`。标签版本必须与安装器版本一致，不带 `+` 构建元数据；不同内容使用新版本，已公开的版本不覆盖。

工作流先验证版本、构建固定版本的 ADBKeyboard，再分别构建 Windows x86_64、macOS Intel 和 Apple Silicon。三个平台全部成功后，发布任务校验每个安装器的版本/平台清单及校验文件，汇总为 `desktop-update.json`，上传安装器、SHA-256、inventory 和汇总清单。上传完成才把草稿转为公开 Release，任何平台失败不会发布可见更新。工作流只在发布任务拥有 `contents: write`；可重试尚未发布的草稿，拒绝覆盖已公开版本。

`workflow_dispatch` 可手动构建测试版本并下载 Actions artifacts，不创建公开 Release。`desktop-v0.2.0-rc.1` 等预览标签生成 prerelease，客户端稳定版检查忽略；develop 和 main 每次推送都会构建唯一的 `ci` 测试版本，产物在 Actions 中下载；这些构建不会创建 Release，也不会通知用户升级。Collector 发布使用自己的标签，客户端不会把 Collector 当作桌面新版。

发布前应分别验收首次安装、旧版升级、后台注册、模型/设备配置与任务运行。macOS 正式公开分发还需要将 Developer ID 签名、公证后的 `.app` 重新归档并重新生成校验文件/清单；当前通用构建工作流没有配置签名证书或公证凭据，产物为未完成正式签名验收的测试包。不要把未签名产物宣称为可无提示自动升级的 macOS 正式版，更新器不会绕过 Gatekeeper。

GitHub 对 `GITHUB_TOKEN` 创建涉及工作流变更的新标签 Release 有额外限制，因此先合并工作流到默认分支再打标签。[GitHub Releases API](https://docs.github.com/en/rest/releases/releases)。

## 发布清单与本地验证

汇总清单 schema 为 `1`，包含版本以及三个目标平台的安装器名称、字节大小、SHA-256。客户端从固定官方仓库 `LordRosenberg/ClickClick` 的已发布 Release 列表筛选最高稳定版本，下载对应 Release 的清单，按本机安装记录选择平台；不使用仓库 `/latest`，因为该仓库也发布 Collector。

构建目录必须包含三个平台的唯一安装器及相邻 inventory、校验文件，然后可离线验证汇总：

```text
python scripts/desktop_release.py --version 0.2.0 --directory build/desktop-release
python -m pytest tests/test_desktop_updates.py tests/test_desktop_onboarding.py -q
```

`--publish --tag desktop-v0.2.0` 会通过 `gh` 写入外部 Release，仅在准备正式发布且具备授权时使用。客户端检查无需 GitHub 令牌；公开 API 限流、网络失败或清单不完整会显示检查错误。

## 更新恢复与验收边界

后台只检查并提示，具体版本需用户确认。每次确认生成唯一系统一次性任务，独立于后台的计划任务/LaunchAgent，更新后不在下次登录重放。状态写入 `data/update-operation.json`，最终准备写入任务接收门禁；更新器和安装器分别持有锁，恢复入口不会在运行中放开门禁。

失败恢复只修改程序版本指针和后台定义，不恢复旧数据库快照；数据库结构升级必须自行保证向后兼容，否则旧版本恢复可能失败。升级会停止旧后台并切换版本，保留必要的回退版本；被旧 MCP 会话占用的目录记录为待清理，释放后重试。

## Collector 独立版本

Collector 源码、构建配置或工作流变更才触发测试 APK 构建，普通主机端改动不会构建 Collector。测试 APK 作为 Actions artifact 保存，不自动成为运行时选择。

维护者发布新版时，更新 Android `versionName` / `versionCode`，验收 APK 后将版本和实际文件 SHA-256 同步到 `driver/collector_release.py` 的 `COLLECTOR_VERSION` / `COLLECTOR_SHA256`，再发布对应 `collector-v<版本>` Release 及同名 APK。不要覆盖已发布的 APK。后续主机端构建使用该固定版本并在下载时验证校验值；用户无需手动选择 Collector 版本。

## 下载趋势

公开仓库每日从 Releases API 读取桌面安装器 asset 的 `download_count`，趋势存放在官网分支的 `stats/`，不触发桌面安装器构建。首次采样之前的逐日历史无法还原；已删除安装器保留最后一次观测数。统计包含重复下载与升级下载，不代表独立用户人数。
