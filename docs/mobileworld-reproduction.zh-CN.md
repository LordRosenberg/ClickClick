# 复现 MobileWorld 纯 GUI 评测

[English](mobileworld-reproduction.md) · [成绩与澄清](mobileworld-results-20261002.md) · [评测架构与升级](mobileworld-evaluation-architecture.md)

公开入口包含完整的 **117 道原题运行与 9 道已披露变体**，使用独立冻结的运行时、提示词和技能。官方环境负责初始化和最终评分，ClickClick 负责模型调用、观察、记忆和 GUI 操作。澄清成绩将九道变体结果全部替换到原题集合中，失败也替换，不按较好结果择优。

## 离线准备

建议 Python 3.12。在仓库根目录创建并激活虚拟环境后运行：

```powershell
python -m venv .venv-mobileworld
.\.venv-mobileworld\Scripts\Activate.ps1
python -m evaluation.mobileworld.reproduce --prepare-only
```

这一步校验源码哈希，将冻结输入复制到 `data/mobileworld-reproduction/input`，不访问设备、Docker 或模型。增加 `--install` 可安装冻结运行时依赖、PyAV 14.0.1 和 Requests；依赖采用公开版本范围，并非完整历史依赖锁。

## 准备专用环境

按[固定上游版本的部署说明](https://github.com/Tongyi-MAI/MobileWorld/tree/e41d1478e252325c513003d3d191b4c164b4af2c)创建专用官方 **v1.4 / Android 14 / API 34** 容器。默认容器名 `mobile_world_env_0`、后端 `http://127.0.0.1:6800`、宿主 ADB 端口 5556。容器内 `emulator-5554` 与宿主同名设备处于不同 ADB 命名空间，宿主操作必须使用显式端点。

检查容器中的 Mattermost 官方 helper 含 `_extend_session_expiry`，Thanksgiving 官方任务含 `reset_chrome` 导入；若缺失，从固定上游版本更新对应官方模块。适配器会拒绝缺少这些修复的环境。保持初始化数据和评分条件不变。在两题之间安装已披露的有界 Mattermost API 就绪等待，再重启容器：

```powershell
Set-Location data/mobileworld-reproduction/input/runtime
python -m evaluation.mobileworld.patch_mattermost_readiness --container mobile_world_env_0 --archive data/readiness-patch --apply
docker restart mobile_world_env_0
docker cp evaluation/mobileworld/adb_proxy.py mobile_world_env_0:/tmp/clickclick_mobileworld_adb_proxy.py
docker exec -d mobile_world_env_0 python3 /tmp/clickclick_mobileworld_adb_proxy.py
adb connect 127.0.0.1:5556
adb -s 127.0.0.1:5556 shell getprop ro.serialno
```

记下最后返回的设备序列号，然后回到仓库根目录。代理桥接容器公开端口与模拟器 ADB 监听端口，重连与重启后仍检查专用目标身份。

按 [Collector 安装说明](accessibility-collector-setup.zh-CN.md)构建公开 [Android 工程](../android/)，并自行提供 [ADBKeyboard APK](https://github.com/senzhk/ADBKeyBoard)。APK 不进入公开仓库。历史 Collector 二进制哈希见[来源记录](../evaluation/mobileworld/reproduction/provenance.json)；从源码重建不意味着 APK 字节一致。入口记录实际两个 APK 的哈希，恢复运行时禁止变更。

按[部署说明](deployment.zh-CN.md)配置自己的模型访问方式；`--env-file` 仅把本地配置复制到被忽略的运行目录。入口固定 `plan_executor`、16,000 历史 token、high reasoning、冻结提示词与技能，并关闭自动压缩尝试笔记。修改 `--model` 的结果属于新模型配置，不能称为已公布的 GPT-5.6-SOL 成绩。

## 完整运行

```powershell
python -m evaluation.mobileworld.reproduce --install `
  --expected-device-serial YOUR_CONTAINER_SERIAL `
  --collector-apk data/device-apks/collector.apk `
  --ime-apk data/device-apks/ADBKeyboard.apk `
  --env-file .env
```

默认先运行 117 道原题，再运行九道变体，每题最多 **50 个预测回合**，终止回答和被拒绝的尝试动作也计入；另有 **2,400 秒本地时限**。模型调用、内部角色交接和物理子动作单独计数。每题维持连续 Agent 会话，执行结束才调用官方评分器。

`--variant original` 只运行 117 道原题；`--variant clarified` 只运行九道变体，单独使用不会生成完整澄清成绩；默认 `paired` 运行两部分。`--backend`、`--target`、`--container` 和 `--environment-device` 可显式覆盖。批次启动前只读校验容器中全部任务实现哈希，实际题目必须与固定配置匹配才会交付。

两道已披露的 Mastodon 题目在两个口径中均提供经检查的官方合成测试账号环境信息；登录仍由 Agent 通过 GUI 完成。第九个变体还将账号信息补入题目正文，不注入 Cookie 或答案。

## 恢复与产物

重复相同命令并加 `--resume`。源码、模型、配置、设备、APK 或口径改变都会被拒绝。保留已评分的失败，不为提高成绩重跑。初始化异常和中断保留诊断，检查原因后再恢复。在当前口径输出目录 `original/` 或 `clarified/` 放入 `STOP` 或 `PAUSE_AFTER_EPISODE` 可在题目边界暂停，恢复前移除标记。

原题与变体成绩、录像、轨迹和健康检查分别保存在 `data/mobileworld-reproduction`。`public-summary.json` 仅输出汇总和完整性；全部 117 道有数值成绩且九道变体齐全后才给出完整成功率。未完成或评分器报错不会自动生成官方分数，应保留首次记录并检查失败。配置、原始轨迹和数据库保留本地，公开前另行审核。

## 冻结范围

[运行配置](../evaluation/mobileworld/reproduction/profile.json)公开任务顺序、两版目标、上游版本和预算，[哈希清单](../evaluation/mobileworld/reproduction/seal.json)校验分发输入。历史输入清单覆盖的 Agent 与设备源码均匹配已归档哈希；提示词和技能来自已评分批次。感知支持文件取自评测时期已审核提交。两个历史 harness 文件未完整保留字节，采用已审核后续副本；公开启动器与两题账号信息注入属于已披露适配，详见来源记录。

完整入口不保证随机模型重跑恰好等于 **106/117 与 112/117**，发布时未重新执行付费全量评测。新成绩应记录实际镜像、APK 构建和依赖版本。
