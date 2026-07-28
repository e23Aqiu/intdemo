# 客户端更新目录

此目录由 API 和 Caddy 共同以 `/updates/` 只读提供。发布脚本会生成：

- `test.json` 或 `stable.json`：以完整包为顶层、包含可选 `deltas` 的版本清单；
- `files/IntDemoOnline-Setup-x.y.z.exe`：对应安装包。
- `files/IntDemoOnline-Patch-a.b.c-to-x.y.z.exe`：可选的精确版本增量包。

服务器上的清单和安装包不进入 Git。先完整上传安装包，再原子替换版本清单，
客户端才会看到新版本。清单接口根据客户端当前版本精确选择增量包；没有匹配
项时始终返回完整包。
