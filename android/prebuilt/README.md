# Prebuilt / Release device APKs

## Collector（本仓库）

| 文件 | Package | 要求 |
|------|---------|------|
| `clickclick-collector-0.2.0-debug.apk` | `ai.clickclick.collector` | Android **8.0+**（API 26） |

### 获取路径

1. **仓库内预编译**：`android/prebuilt/clickclick-collector-*.apk`
2. **GitHub Release**（公开 / 私有仓各有一份）  
   - 公开：https://github.com/LordRosenberg/ClickClick/releases  
   - 私有：https://github.com/LordRosenberg/ClickClick-Dev/releases  
3. **自己编译** `android/accessibility-collector/` 后拷贝到本目录

### 维护者：双仓日常发布

```bash
# 私有仓：push develop；若本地 collector APK digest 变了 → 更新 Dev Release
./devtools/push-develop.sh

# 公开仓：develop → main（去掉 openspec/devtools/evaluation）→ push public；
# 若 APK digest 变了 → 更新公开 Release
./devtools/sync-public.sh
```

强制重传 Release：`./devtools/publish-collector-release.sh both`

一键装机：`./scripts/bootstrap-device.sh`
