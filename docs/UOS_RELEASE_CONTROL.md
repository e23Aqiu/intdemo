# 统信主控双端构建与发布

本方案推荐以 UOS Desktop 20 ARM64 真机作为发布主控：DEB 在本机生成，Windows
EXE 优先由 GitHub Actions 生成，GitHub 不可用时由任意 Windows x64 真机完成构建
任务。Windows 也可以作为主控：把 UOS 生成的标准结果 ZIP 带到 Windows 导入后，
可由 Windows 校验两个结果并统一发布。

发布机不需要 Docker。Docker 只用于部署 IntDemo 服务端；以后即使把服务端迁移到
这台 UOS，打包发布器与服务端容器仍是两套独立环境。

## 1. 网络和机器边界

| 机器 | 必须能够访问 | 不要求访问 |
| --- | --- | --- |
| UOS ARM64 发布机 | GitHub/Gitee（同步代码）、更新服务器 HTTPS、发布服务器 SSH | Docker（只打包发布时） |
| GitHub Actions | GitHub 仓库、Python/构建依赖 | IntDemo 程序服务器、发布服务器 |
| 个人或站点 Windows x64 | GitHub/Gitee、Python/构建依赖（首次准备时） | IntDemo 程序服务器、发布服务器 |
| Windows 发布主控 | GitHub/Gitee、更新服务器 HTTPS、发布服务器 SSH | UOS 本机构建环境（导入 UOS 结果 ZIP 时） |

因此个人 Windows 即使使用 `198.x` 网络，无法访问只允许公司 `121.x` 的程序
服务器，仍可拉取精确 Git 提交并构建 EXE。任务中的 HTTPS 地址和公开 CA 根证书
只会写进安装包，不会在构建时请求该地址。构建完成后的结果 ZIP 可通过 GitHub、
Gitee、U 盘或单位批准的传输介质带回 UOS。

任务包不会包含 SSH 私钥、CA 私钥、服务器密码或 GitHub 令牌。需要随客户端发布
私有 CA 时，只允许放入公开的 CA 根证书。

## 2. UOS 发布机准备

先完成 ARM64 环境准备，并建议安装 UOS 系统文件选择组件：

```bash
bash scripts/uos-arm64/prepare-env.sh
sudo apt install zenity
git remote -v
```

使用 GitHub 自动构建时，另外安装 ARM64 版 GitHub CLI，并完成登录：

```bash
gh --version
gh auth login --hostname github.com
gh auth status --hostname github.com
```

正式发布需要系统提供 `ssh`、`scp`，并允许 UOS 发布机访问发布服务器。执行“运行
全部测试”或一键完整流程时，还要准备 `server/.venv`；只构建 DEB、导出/导入
Windows 任务时不需要 Docker。

从 UOS 图形桌面的终端启动发布器：

```bash
bash scripts/uos-arm64/run-release-publisher.sh
```

UOS 配置保存在：

```text
~/.config/intdemo-release-publisher/settings.json
```

配置只保存地址和文件路径（包括 Gitee 仓库链接），不保存更新说明、强更选择、
密码、令牌或私钥内容。

## 3. 构建方式

“Windows x64”和“UOS ARM64”可以单选，也可以全选。再通过“输出类型”选择：

- `仅安装包（EXE / DEB）`：只导出可直接安装的安装包；
- `仅构建包（发布器结果 ZIP）`：只导出用于另一台打包器导入的结果 ZIP。UOS
  需要先有同版本 DEB；Windows 会在生成结果 ZIP 的过程中构建 EXE，EXE 会作为
  结果包内的校验产物；
- `安装包 + 构建包`：两类都导出，正式双端发布推荐使用。

正式双端发布始终要求同一版本、同一 Git 提交和同一在线配置的 Windows/UOS 校验
收据都已经生成。

Windows 构建方式有三种：

- `自动`：先导出可复用任务 ZIP，再尝试 GitHub Actions；任何 GitHub 连接、登录、
  等待或下载问题都会保留任务 ZIP，并提示转 Windows 真机。
- `仅使用 GitHub Actions`：缺少 `gh` 或 GitHub 不可用会直接作为环境/步骤错误。
- `Windows 真机任务包`：只导出任务 ZIP，不尝试 GitHub。

“同时生成 Windows 便携包”默认关闭；偶尔需要便携版时再勾选，Windows 安装包或结果
包会额外包含便携 ZIP。导入 Windows 构建包时，发布器会校验结果元数据和文件清单，自动识别
是否含便携包并同步界面选项。Windows 增量包只在填写精确来源版本时生成；UOS
始终发布完整 DEB。

## 4. 推荐的 GitHub 双端流程

1. 在 UOS 发布器中同步目标版本，检查改动并提交。
2. 填写 Gitee 仓库链接，点击“推送 GitHub + Gitee”，把同一分支普通推送到
   `origin` 和 `gitee`。发布器会在缺少或地址变化时配置 `gitee` 远程，不会强制推送。
3. 填写 HTTPS 服务地址、公开 CA 根证书、通道、更新说明、SSH 主机和更新目录。
4. 选择 Windows x64 与 UOS ARM64，Windows 构建方式选择“自动”。
5. 需要增量包时填写已经真实发布过、且本机有发布快照的来源版本。
6. 选择“安装包 + 构建包”，点击“环境检查”，再点击“导出 EXE + DEB（安装包 +
   构建包）”。

构建顺序如下：

1. UOS 本机执行 ARM64 完整检查并生成 DEB。
2. 发布器核对 DEB 的版本、Git 提交、服务地址、通道、CA 和 SHA-256，写入 UOS
   构建收据并生成可跨机器导入的 UOS 结果 ZIP。
3. 发布器导出 Windows 请求 ZIP，创建临时 Git 标签和 GitHub prerelease 资源。
4. GitHub Windows runner 运行完整客户端测试，构建 EXE、可选增量包/便携包和本次
   Windows 快照，上传一个结果 ZIP。
5. UOS 下载结果，核对请求 SHA-256、源提交、版本、服务地址、通道、CA、产物集合、
   文件名、大小、SHA-256 和快照内容，写入 Windows 构建收据。
6. 导入成功后清理临时 GitHub release 和标签；清理失败只产生警告，不改变已经
   校验的本地结果。

主要本地产物位于：

```text
dist/uos-arm64/IntDemo-UOS-arm64-<版本>.deb
dist/uos-build-results/<版本>/validated-result.json
dist/uos-build-results/<版本>/uos-build-result-<版本>-<提交前12位>.zip
dist/installer/IntDemoOnline-Setup-<版本>.exe
dist/windows-build-results/<版本>/validated-result.json
dist/windows-build-results/<版本>/windows-build-result-*.zip
dist/windows-build-requests/<版本>/<任务ID>/windows-build-request-*.zip
```

GitHub Actions 已经启动但测试或构建失败时，不会伪装成网络故障自动降级。应先在
Actions 日志中修复真实失败，再重新提交和构建。

## 5. GitHub 不可用时转 Windows 真机

“自动”方式显示“需要 Windows 真机构建”后，在 `dist/windows-build-requests/` 中
取最新的请求 ZIP；也可以提前把“Windows 构建”改为“Windows 真机任务包”，再点击
主导出按钮。ZIP 内的
`BUILD-WINDOWS.txt` 记录了必须使用的完整 Git 提交。

Windows x64 真机需要 Git、64 位 Python 3.9 和 Inno Setup 6。先获取完全相同的
代码提交，再从仓库根目录执行任务：

```powershell
git clone https://github.com/e23Aqiu/intdemo.git
cd intdemo
git fetch --all --tags
git checkout --detach <BUILD-WINDOWS.txt 中的完整提交>
winget install --id JRSoftware.InnoSetup --exact
powershell -ExecutionPolicy Bypass -File .\scripts\build-windows-request.ps1 `
  -RequestArchive "D:\transfer\windows-build-request-....zip"
```

辅助脚本会创建 `.venv`、安装开发依赖、运行完整客户端测试并生成：

```text
dist\windows-manual-results\windows-build-result-<时间>.zip
```

已经准备好正确依赖的离线 Windows 环境可加 `-SkipDependencyInstall`，但不能跳过
脚本内的完整测试。把结果 ZIP 带回 UOS 或 Windows，在保持服务地址、通道、CA 和
增量来源与导出时完全一致的情况下，点击“导入 Windows 构建包”。便携包选项会从结果中自动
识别并同步；其他配置或文件发生变化仍会拒绝导入，不能通过重命名 ZIP 绕过。

UOS 本机构建生成的 `uos-build-result-*.zip` 也可以通过 U 盘或单位批准的传输介质
带到 Windows，在 Windows 发布器点击“导入统信构建包”。导入会核对完整 Git 提交、
版本、服务地址、通道、CA 指纹、文件名、大小和 SHA-256。裸 DEB 仍可直接安装，
但正式双端发布应导入结果 ZIP，以保留构建来源和配置校验。

## 6. 从 Windows 或 UOS 正式发布

任一主控完成构建和导入后都可点击“双端发布”；能直接构建两端结果时也可使用
“测试 -> 双端构建 -> 双端发布”一键流程。正式发布必须满足：

- Git 工作区干净，当前版本与两个收据的完整 Git 提交一致；
- EXE 与 DEB 的版本、服务地址、通道和 CA 指纹一致；
- Windows 增量来源及便携包选择与收据一致；
- 当前 Windows/UOS 主控能访问更新 API，并且 API 已支持
  `X-IntDemo-Platform` 双端清单；
- 已填写可用的发布服务器 SSH 主机、更新目录及可选 SSH 私钥。

发布器先检查远程通道不能覆盖同版本或更高版本，再按 SHA-256 上传缺少的产物。
远程已有同名但不同哈希的文件会被拒绝。双端清单最后原子安装，并带并发变更
保护；远程清单完整哈希验证成功后，才把 Windows 本次快照保存为正式发布快照。

发布主控必须能访问程序服务器和发布服务器 SSH，但仅负责构建的 Windows/UOS
机器不需要。以后服务器迁移到
UOS 并只允许 `121.x` 内网时，只要 UOS 发布机位于允许网段，个人 Windows 仍可
照常构建 EXE 并传回结果。

## 7. 增量快照规则

`dist/release-snapshots/<来源版本>.json` 必须来自当时真正成功发布到服务器的
Windows 候选包。不能切换到旧 Git 提交重新构建一个“看起来相同”的旧版本作为
基线，因为依赖解析、构建工具和二进制内容可能已经变化。

导出 Windows 任务时，发布器会把这份真实快照连同 SHA-256 放入任务 ZIP；Windows
只据此计算差异。新版本的候选快照先随结果 ZIP 回到 UOS，只有远程发布全部成功后
才进入 `dist/release-snapshots/`，失败发布不会污染下一次增量基线。

## 8. 常见问题

- 提示 GitHub 暂不可用：任务 ZIP 已保留，检查 `gh auth status`，或直接走 Windows
  真机流程。
- 提示提交尚未推送：先在发布器点击“推送”，确认 GitHub 对应分支指向当前提交。
- 导入提示配置不一致：把 UOS 界面恢复为导出任务时的服务地址、通道、CA、增量
  来源和便携包选择，不能修改结果文件。
- Windows 需要发布 UOS 包：从 UOS 取得 `uos-build-result-*.zip`，不要只复制裸
  DEB；在 Windows 勾选 UOS ARM64 后点击“导入统信构建包”。
- 缺少增量来源快照：从保存真实发布资料的 UOS 发布机恢复该版本快照；不要重建。
- 发布 API 不支持双端平台：先部署当前服务端更新清单接口，再发布双端安装包。
- UOS 没有 Docker：打包和 SSH 发布不受影响；只有在本机部署服务端时才安装
  Docker Engine 与 Compose。
