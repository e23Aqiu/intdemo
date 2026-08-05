# 逃费车辆信息智能查询平台双端打包发布器

发布器现在以 UOS ARM64 真机作为推荐主控：DEB 在本机生成，Windows EXE 优先由
GitHub Actions 生成；GitHub 不可用时可把带哈希的任务 ZIP 交给个人或站点
Windows x64 真机构建，再回到 UOS 校验并统一发布。原有 Windows 主控流程继续
保留。完整的 UOS 操作手册见
[`UOS_RELEASE_CONTROL.md`](UOS_RELEASE_CONTROL.md)。发布器不会被打进业务客户端，
也不会向业务服务器开放构建命令执行接口。

## 1. 启动

UOS 主控先准备 ARM64 环境，然后从图形桌面的终端启动：

```bash
bash scripts/uos-arm64/prepare-env.sh
bash scripts/uos-arm64/run-release-publisher.sh
```

GitHub 自动构建还需要 ARM64 版 `gh` 并执行 `gh auth login`。UOS 本机只打包和
发布时不需要 Docker；执行完整测试流程时需要项目的 `server/.venv`。

Windows 主控或 Windows 真机构建任务需要项目虚拟环境与 Inno Setup 6：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\server\.venv\Scripts\python.exe -m pip install -e ".\server[test]"
winget install --id JRSoftware.InnoSetup --exact
```

Windows 主控可双击仓库根目录的 `run-release-publisher.bat`，或在 PowerShell 中
运行：

```powershell
.\run-release-publisher.bat
```

发布器使用 `origin` 作为 GitHub 远程，并提供“Gitee 仓库”输入框。输入链接后点击
“推送 GitHub + Gitee”，发布器会安全地新增或更新本仓库的 `gitee` 远程；也可以
先手动检查：

```powershell
git remote -v
git remote -v
```

发布器配置分别保存在：

```text
Windows: %LOCALAPPDATA%\IntDemoReleasePublisher\settings.json
UOS:     ~/.config/intdemo-release-publisher/settings.json
```

这里只保存服务地址、文件路径、SSH 主机和发布目录等开发配置。更新说明、
“强制更新”选择以及任何密码、令牌或私钥内容都不会保存。

## 2. 推荐的新版本流程

1. 确认 Git 工作区干净，填写目标版本并点击“同步项目版本号”。
2. 检查修改，运行测试，点击“提交到本地”创建本地提交。
3. 填写 Gitee 仓库链接，点击“推送 GitHub + Gitee”，把当前分支普通推送到
   `origin`（GitHub）和 `gitee`（Gitee）。
4. 填写服务地址、公开 CA 根证书、发布通道、更新说明和发布服务器 SSH 配置。
5. 选择 Windows x64、UOS ARM64 或两者；UOS 主控的 Windows 构建默认选择“自动”。
6. 如果需要增量包，填写本机已有真实发布快照的精确来源版本。
7. 点击“环境检查”，再按需运行测试或“构建所选安装包”。
8. GitHub 不可用时，把保留的任务 ZIP 带到 Windows x64 运行
   `scripts/build-windows-request.ps1`，然后回 UOS 点击“导入 Windows 结果”。
9. 两个平台收据都校验通过后，点击“双端发布”；GitHub 可用时也可使用一键流程。

版本区会持续显示“暂无结果”“仅 EXE/仅 DEB 就绪”“双端均已就绪”或“已发布”，
避免构建结果留在 `dist` 中而忘记发布。单平台构建仍可独立执行；正式发布继续要求
同版本双端结果，防止其中一端收到新版本号却下载旧安装包。

单平台构建允许用于验证，但正式发布必须同时选择同版本 EXE 与 DEB。UOS 正式发布
要求 Git 工作区干净并填写 SSH 主机；服务器上的安装包都可追溯到明确提交。

当前业务客户端只检查 `test` 通道。发布器中的 `stable` 是为后续正式通道
预留的选项，现有客户端不会自动读取该通道。

## 3. 各操作的含义

### 环境检查

检查以下内容：

- 客户端与服务端虚拟环境；
- 项目内各处版本号是否一致；
- HTTPS 服务地址和 IP 私有 CA；
- Windows 本地主控所需的 Inno Setup，或 UOS 主控的 GitHub/真机构建配置；
- 非 UOS 主控使用的 ARM64 SSH 构建机，或 UOS 本机构建环境；
- 增量来源版本及其发布快照；
- SSH 主机、私钥和远程目录格式；
- 发布产物及已发布版本不可覆盖规则。

### 运行全部测试

依次执行：

```text
客户端 compileall
客户端 pytest
服务端 pytest
```

任何一步失败都会停止后续步骤。

### 提交变更

先读取并展示当前 Git 工作区变更，填写提交说明并再次确认后，依次执行
`git add --all` 和 `git commit`。因此修改、删除和未跟踪文件都会进入本次
本地提交；操作不会自动推送到 GitHub 或 Gitee。

### 推送

要求 Git 工作区已经提交且当前分支没有落后已知远程跟踪分支。操作前分别显示
`origin/<分支>` 与 `gitee/<分支>` 的待推送提交，再依次执行普通 `git push`；
首次推送只把 `origin` 设置为上游，Gitee 保持镜像远程。不会使用强制推送。

### 客户端断连测试

“客户端断连测试”使用服务地址、CA 以及临时输入的服务器管理员账号登录。
管理员密码和访问令牌只保留在当前发布器进程内，不会写入
`settings.json`。点击“刷新”可查看服务器登记的业务客户端，并执行：

- 断开或恢复当前选择的客户端；
- 断开全部业务客户端，同时保留发布器自身的恢复控制连接；
- 恢复全部连接。

断开操作只临时阻断业务 API、登录/刷新、数据同步和 WebSocket，不会撤销
设备、删除离线授权、清空账号资料或丢弃本机待同步数据。客户端会把该状态
作为服务器不可用处理；恢复后可继续使用原会话并上传积压数据。服务端进程
重启时测试闸门自动恢复为全部放行，避免遗留阻断。

指定客户端依赖已认证的设备身份，因此匿名更新清单和由 Caddy 提供的安装包
下载不在指定断连范围内。该功能用于验证业务客户端离线行为，不等同于关闭
服务器网络或停止 Caddy。

### 构建安装包

UOS 主控可以单选或全选 Windows x64 与 UOS ARM64。DEB 由
`scripts/uos-arm64/build.sh` 在本机生成并写入带提交、在线配置和哈希的收据。
Windows 默认先导出不可变任务，再由 GitHub Actions 构建并自动导入；GitHub
不可用返回专用提示码，任务 ZIP 保留给 Windows 真机使用。Windows 真机只需访问
代码和依赖来源，构建过程不连接 IntDemo 程序服务器。

Windows 主控继续复用 `scripts/build-releases.ps1`；非 UOS 主控仍可通过
`scripts/build-uos-remote.ps1` 连接 ARM64 构建机。PyInstaller 不能跨架构编译，
所以任何 DEB 都必须在 ARM64 Linux 真机生成。

### 发布

UOS 主控先重新校验两个平台收据及其中每个文件，再检查更新 API 已启用平台选择，
通过 SSH 上传 `.exe`、可选 Windows 增量包和 `.deb`，最后原子替换双端清单。远程
同名文件只有 SHA-256 完全一致时才复用，否则拒绝覆盖；清单替换带并发保护，并在
远程完整哈希验证成功后才保存本次 Windows 发布快照。UOS 正式发布不能省略 SSH
主机。

Windows 主控继续复用 `scripts/publish-update.ps1`，并保留 SSH 主机为空时仅生成
本地 `dist/update-release` 的兼容行为。

已经存在 `dist/release-snapshots/<版本>.json` 的版本视为已发布版本，发布器
不会覆盖。发现已发布版本有问题时，应暂停服务器上的该版本并发布更高版本的
修复包，不应原地替换同版本二进制文件。

### 暂停分发

“暂停分发”只作用于所选 SSH 远程通道，不要求重新构建安装包，也不受本地
待发布版本和 Git 工作区状态影响。确认后，发布器调用
对应平台的暂停任务：

1. 读取并校验当前活动清单，前后两次核对 SHA-256，避免与并发发布互相覆盖；
2. 把原清单完整复制到更新目录同级的 `<更新目录>-paused/`，该目录不由
   Caddy 对外提供；
3. 原子安装兼容旧客户端的暂停标记，服务器上的安装包保持不变。

暂停后，新的更新检查会得到“暂无更新”，不会显示下载错误。已经取得旧清单
或正在下载的客户端不会被强制中断。下一次发布更高版本时，发布脚本会按照
暂停前的真实版本继续执行防覆盖检查，并在新清单原子替换成功后自动恢复分发。
相同或更低版本仍然不能覆盖。

## 4. 增量包规则

增量包目前只用于 Windows；UOS 每次发布完整 DEB。Windows 增量来源版本必须满足：

- 使用 `x.y.z` 格式；
- 低于目标版本；
- `dist/release-snapshots/<来源版本>.json` 存在且来自当时真实成功发布的候选包；
- 安装端注册表版本与来源版本精确一致。

任务导出会把真实来源快照及其 SHA-256 一并交给 Windows 构建机。不能通过切换到
旧提交重新构建旧版本来替代发布快照。发布器不会读取或比较用户数据库、Excel
或其他业务文件；差异比较只发生在正式程序文件清单之间。

## 5. 发布安全

- 远程发布前必须再次人工确认版本、通道、包类型和强更标记。
- 发布清单记录源代码 Git 提交；远程发布时脚本会再次拒绝未提交工作区。
- Windows 请求与结果绑定源提交、版本、配置和哈希，导入时逐项重新校验。
- 任务包只可包含公开 CA 根证书，不包含 CA 私钥、SSH 私钥或访问令牌。
- 发布脚本校验文件 SHA-256，并在安装包到位后才替换版本清单。
- 远程通道已经发布相同或更高版本时，脚本拒绝覆盖或降级。
- 暂停分发会归档原清单并用 SHA-256 条件更新，安装包和归档清单均不会删除。
- 停止流程会终止当前本地命令及其子进程；已经上传或生成的文件不会自动删除。
- 客户端断连测试需要服务器管理员权限，所有断开/恢复操作均写入服务端审计；
  服务端重启会自动解除测试阻断。
- 正式商用自动更新仍建议增加 Windows Authenticode 代码签名校验。
