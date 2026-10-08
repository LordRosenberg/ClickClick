ClickClick 0.2.0 adds task-conditioned skill learning: continuous trajectory analysis, independent Skill Reviewer checks, multiple app-owned candidate files, joint verification reuse and local admission per file. Review candidates in Skills → Pending before approving them. Learning requires explicit budget and device authorization; experimental skills are not automatically installed.

Includes offline Windows x86_64, macOS Apple Silicon and macOS Intel installers. Check each installer's adjacent SHA-256 file; `desktop-update.json` contains the verified platform manifest. Desktop setup and assistant MCP connection are described in the [installation guide](https://github.com/LordRosenberg/ClickClick/blob/main/docs/desktop-setup.zh-CN.md).

**macOS limitation:** These two macOS packages are not Developer ID signed or Apple notarized. Gatekeeper may prevent opening them. This release does not claim notarization or advise disabling system protection; source deployment remains available. A future version will add signing and notarization when the required Apple Developer credentials are available.

---

ClickClick 0.2.0 增加基于任务轨迹的 skill 学习：连续分析、独立 Reviewer 审查、多应用候选、联合验证复用和逐文件局部验收。在“技能 → 待审 Pending”检查后再批准；学习需要明确授权预算和设备操作，实验 skill 不会自动安装。

提供 Windows x86_64、macOS Apple Silicon 与 Intel 离线安装器，各有 SHA-256 和平台清单。

**macOS 限制：本次两种 macOS 包未经过 Developer ID 签名和 Apple 公证，可能被 Gatekeeper 阻止打开。** 不要求关闭系统保护；可选择源码部署。取得所需 Apple Developer 凭据后，后续版本再补齐签名公证。
