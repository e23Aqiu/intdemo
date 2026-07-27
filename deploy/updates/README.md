# 客户端更新目录

此目录由 Caddy 以 `/updates/` 只读提供。发布脚本会生成：

- `test.json` 或 `stable.json`：版本清单；
- `files/IntDemoOnline-Setup-x.y.z.exe`：对应安装包。

服务器上的清单和安装包不进入 Git。先完整上传安装包，再原子替换版本清单，
客户端才会看到新版本。
