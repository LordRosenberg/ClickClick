# Prebuilt / Release device APKs

## Collector（本仓库）

| 文件 | Package | 要求 |
|------|---------|------|
| `clickclick-collector-0.2.0-debug.apk` | `ai.clickclick.collector` | Android **8.0+**（API 26） |

两条获取路径（任选）：

1. **仓库内预编译**（clone 后直接用）  
   `android/prebuilt/clickclick-collector-0.2.0-debug.apk`

2. **GitHub Release 下载**  
   - Release：[`collector-v0.2.0`](https://github.com/LordRosenberg/ClickClick/releases/tag/collector-v0.2.0)  
   - Asset：`clickclick-collector-0.2.0-debug.apk`  
   - 直链：  
     `https://github.com/LordRosenberg/ClickClick/releases/download/collector-v0.2.0/clickclick-collector-0.2.0-debug.apk`

3. **自己编译**（改源码后）  

```sh
# Android Studio 打开 android/accessibility-collector/，或 Gradle assembleDebug
cp android/accessibility-collector/app/build/outputs/apk/debug/app-debug.apk \
  android/prebuilt/clickclick-collector-0.2.0-debug.apk
```

发新版到公开仓 Release（维护者）：

```sh
./devtools/publish-collector-release.sh 0.2.0
```

## ADBKeyboard（第三方）

不 vendoring。默认从 [senzhk/ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard) 拉取 `ADBKeyboard.apk`。  
同一 Release 也会附带一份副本，便于国内网络：`ADBKeyboard.apk`。

## 一键安装

```sh
./scripts/bootstrap-device.sh
```
