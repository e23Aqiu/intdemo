# 客户端更新目录

此目录由 API 和 Caddy 共同以 `/updates/` 只读提供。发布脚本会同时生成 Windows x86_64
与统信 UOS ARM64 更新：

- `test.json` 或 `stable.json`：包含 `windows-x86_64` 和 `linux-aarch64` 两个平台项的版本清单；
- `files/IntDemoOnline-Setup-x.y.z.exe`：Windows 完整安装包；
- `files/intdemo-client_x.y.z_arm64.deb`：统信 UOS ARM64 完整安装包；
- `files/IntDemoOnline-Patch-a.b.c-to-x.y.z.exe`：可选的 Windows 精确版本增量包。

为兼容已经发布的旧 Windows 客户端，清单顶层仍保留 Windows 完整包字段。新客户端通过
`X-IntDemo-Platform` 请求头选择平台；服务端也会在响应中返回 `selected_platform`。

服务器上的清单和安装包不进入 Git。先完整上传安装包，再原子替换版本清单，
客户端才会看到新版本。清单接口先按平台选择更新，再根据 Windows 客户端当前版本精确
选择增量包；没有匹配项时始终返回对应平台的完整包。UOS ARM64 当前只发布完整 `.deb` 包。

发布器暂停分发时会把原活动清单归档到此目录同级的 `updates-paused/`，并用
兼容旧客户端的暂停标记原子替换通道清单。API 对带版本的更新客户端返回与其
当前版本相同的“暂停”响应，对无版本请求返回 HTTP 204；安装包不会删除。
发布更高版本会替换暂停标记并恢复分发。
