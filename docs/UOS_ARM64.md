# 统信 UOS 20 ARM64 构建与真机验收

本文针对以下兼容基线：

| 项目 | 目标值 |
| --- | --- |
| 操作系统 | UOS Desktop 20 Professional 1070，Build 11014.100.100 |
| CPU/包架构 | `aarch64` / `arm64` |
| glibc | 2.28 |
| 系统 Python | 3.7.3（保留给系统，不用于构建） |
| 桌面会话 | Wayland，默认通过 XWayland 运行 Qt 5 和 Chromium |

## 兼容方案

- 不替换、不升级 `/usr/bin/python3`。项目使用 Miniforge 在仓库内创建独立
  Python 3.10 ARM64 环境。
- PyQt5 从 conda-forge 安装。PyPI 的 PyQt5 没有 Linux ARM64 wheel，直接
  `pip install -r requirements.txt` 会失败或进入本地源码编译。
- 环境准备脚本把与固定 Playwright 版本匹配的 ARM64 Chromium 下载到仓库下的
  `.playwright-uos-arm64`。该浏览器已在本文目标真机上通过 Chromium 145 启动、
  AArch64 架构、动态库和 Playwright 控制检查，构建时会自动装入最终软件包。
  `INTDEMO_CHROMIUM_PATH` 仅作为故障排查时的显式覆盖入口。
- Windows DPAPI 在 Linux 上替换为 Secret Service。应用只把随机主密钥放入
  当前用户的桌面密钥环，SQLite 中的在线令牌和离线授权仍然是 AES-GCM 密文。
- 成品包预置在线测试服务 `https://43.138.177.65` 和用于校验该服务证书的公开
  Caddy 根证书。启动器优先使用用户配置，未配置时才使用随包默认值；账号密码、
  令牌和私钥均不进入软件包。
- UOS 包必须在这台 ARM64 真机上构建。PyInstaller 不是交叉编译器；同时在
  glibc 2.28 上构建可以避免产物误依赖更新的 glibc。

## 一、准备系统依赖

先确认当前用户可以使用 `sudo`，然后安装基础工具和常见桌面运行库：

```bash
sudo apt update
sudo apt install -y \
  wget ca-certificates git binutils fakeroot lsof libsecret-tools \
  libglib2.0-0 libdbus-1-3 libfontconfig1 libfreetype6 \
  libx11-6 libx11-xcb1 libxcb1 libxcb-xinerama0 libxkbcommon-x11-0 \
  libxrender1 libxi6 libxrandr2 libxfixes3 libxcursor1 \
  libgl1 libegl1 libgbm1 libnss3 libasound2 fonts-noto-cjk \
  fcitx-frontend-qt5 policykit-1 xdg-utils
```

不同 UOS 补丁级别可能已预装其中一部分；`apt` 会跳过已安装的软件包。如果
个别包名在当前仓库不存在，先保留终端输出，继续运行后面的诊断脚本，它会
指出实际缺失项。

最终用户无需安装 Chromium/UOS 浏览器。首次准备构建环境时需要联网下载约数百
MB 的浏览器文件；网络较慢时可设置代理或增大
`PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT`。

## 二、获取兼容分支

```bash
git clone --branch codex/uos-arm64-compat --single-branch \
  https://github.com/e23Aqiu/intdemo.git
cd intdemo
```

已克隆过仓库时：

```bash
git fetch origin
git checkout codex/uos-arm64-compat
git pull --ff-only
```

## 三、安装 ARM64 Miniforge

Miniforge 的 Linux ARM64 安装器支持 glibc 2.17 及以上，因此覆盖本机的
glibc 2.28 基线。它与 UOS 系统 Python 相互隔离。

```bash
wget -O Miniforge3-Linux-aarch64.sh \
  https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
bash Miniforge3-Linux-aarch64.sh
```

安装完成后重新打开终端，确认 `conda --version` 可用。不要把项目依赖安装到
Miniforge 的 `base` 环境。

## 四、创建项目环境并诊断

```bash
bash scripts/uos-arm64/prepare-env.sh
```

脚本会在仓库下创建 `.conda-uos-arm64`，其中包含 Python 3.10、ARM64
PyQt5、PyInstaller 和项目依赖，并把 Chromium 下载到被 Git 忽略的
`.playwright-uos-arm64`。然后运行完整诊断：

```bash
bash scripts/uos-arm64/diagnose.sh 2>&1 | tee uos-arm64-diagnose.log
```

诊断会自动使用项目缓存，检查 Chromium ELF 架构和缺失动态库，实际启动一次
无头 Chromium，并对 Linux Secret Service 做一轮加解密。首次访问桌面密钥环
时，UOS 可能弹出解锁提示，这是预期行为。

若只想先验证源码界面：

```bash
conda run -p ./.conda-uos-arm64 python main.py
```

从源码启动时，可把仓库中经过验证的在线测试配置复制到用户配置目录：

```bash
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/intdemo-client"
mkdir -p "$config_dir/certs"
cp packaging/uos-arm64/client-online.json "$config_dir/"
cp packaging/uos-arm64/certs/intdemo-caddy-root.crt "$config_dir/certs/"
chmod 0600 "$config_dir/client-online.json"
chmod 0644 "$config_dir/certs/intdemo-caddy-root.crt"
```

该配置指向 `https://43.138.177.65`，`ca_bundle` 使用相对于配置文件的证书路径，
因此复制整个配置目录后仍然有效。若需要改用其他服务，可以编辑用户配置；
客户端不会允许 `http://`、缺少受信 CA 的公网 IP 或 `verify=False` 式绕过。

Wayland 会话且存在 XWayland 时，程序默认使用 `QT_QPA_PLATFORM=xcb` 和
Chromium `--ozone-platform=x11`。如果真机已经安装完整 Qt Wayland 插件并想
验证原生 Wayland，可显式覆盖：

```bash
export INTDEMO_QT_QPA_PLATFORM=wayland
export INTDEMO_CHROMIUM_OZONE_PLATFORM=wayland
conda run -p ./.conda-uos-arm64 python main.py
```

UOS 菜单启动器可能自动注入 `QT_QPA_PLATFORM=wayland`，但当前随包 Qt 不包含
Wayland 平台插件；根启动器会把这个系统注入值改为 `xcb`。普通用户不要直接用
`QT_QPA_PLATFORM=wayland` 覆盖，原生 Wayland 验证统一使用上述项目专用变量。
构建脚本会校验 UOS 的 `fcitx-frontend-qt5` 平台输入上下文及依赖；程序在
PyInstaller 自带 Qt 插件路径之后追加受信任的系统 Qt 插件目录。启动器会尊重
已有的 IBus/Fcitx 配置，并在 UOS 默认场景设置 `QT_IM_MODULE=fcitx`，从而支持
公告标题、轮播文字和富文本正文的中文组合输入。

## 五、构建 ARM64 包

```bash
bash scripts/uos-arm64/build.sh
```

发布器远程构建时会额外传入 `--base-url`、`--channel` 以及 `--ca-bundle`（或
`--no-ca-bundle`），保证 Windows 与 UOS 包使用同一在线服务和证书配置。

默认流程依次执行：环境和浏览器缓存更新、UOS/架构/glibc/依赖/浏览器/密钥环
预检、客户端离线回归测试、PyInstaller 目录包构建、conda ARM64 GNU 运行库
固定、Qt 所需 `GLIBCXX_3.4.26` 与动态库检查、成品入口自检、内置 Chromium
复制与二次启动检查、`tar.gz` 打包、UOS ARM64 DEB 组装和 SHA-256 生成。构建
脚本拒绝打包项目缓存目录之外的浏览器，避免误带入开发机上的其他可执行文件。
正式构建开始前会删除同版本旧压缩包、旧 DEB 和旧组装目录；任何后续检查失败时
都不会留下可被误认为新包的同格式产物。

仅在定位问题时使用跳过参数：

```bash
bash scripts/uos-arm64/build.sh --skip-env-update --skip-tests
bash scripts/uos-arm64/build.sh --skip-browser-check
bash scripts/uos-arm64/build.sh --skip-deb
```

成功后得到：

```text
dist/uos-arm64/IntDemo-UOS-arm64-<版本>.tar.gz
dist/uos-arm64/IntDemo-UOS-arm64-<版本>.tar.gz.sha256
dist/uos-arm64/IntDemo-UOS-arm64-<版本>.deb
dist/uos-arm64/IntDemo-UOS-arm64-<版本>.deb.sha256
```

如果 ARM64 便携包已经成功生成，只想快速补做 DEB，无需重新运行 PyInstaller：

```bash
bash scripts/uos-arm64/build-deb.sh
```

DEB 使用包名 `com.e23aqiu.intdemo`、架构 `arm64`，应用文件位于
`/opt/apps/com.e23aqiu.intdemo/`。包中不使用 `postinst` 修改系统，程序仍以
普通桌面用户运行；用户数据库、Secret Service 密钥和在线配置不会装入 DEB。
DEB 的桌面文件严格使用与 AppID 相同的 `com.e23aqiu.intdemo.desktop`，Qt 的
DesktopFileName 也使用同一值，以便 UOS 应用注册与沙箱识别该入口。菜单名称为
“逃费车辆信息智能查询平台（UOS）”，并在旧版 DDE 下关闭启动通知。

压缩包内的 `browser/` 是完整 Chromium 运行目录；启动器会自动设置
`INTDEMO_CHROMIUM_PATH`，最终用户不需要执行 `playwright install`。
`app/_internal/libstdc++.so.6` 和 `libgcc_s.so.1` 来自项目 conda 环境，避免
PyInstaller 误收集 UOS 系统旧库后无法加载 conda-forge Qt。

压缩包根目录还包含 `client-online.json` 和 `certs/intdemo-caddy-root.crt`。
根启动器按“`INTDEMO_CONNECTION_CONFIG` 显式指定、用户配置、随包配置”的顺序
选择连接配置，所以直接解压试运行即可连接在线测试服务，同时不会覆盖已有环境。

## 六、便携试运行与当前用户安装

```bash
cd /tmp
tar -xzf /项目路径/dist/uos-arm64/IntDemo-UOS-arm64-*.tar.gz
cd IntDemo-UOS-arm64-*
./intdemo-client
```

试运行通过后安装到当前用户，无需 root：

```bash
./install-user.sh
```

程序安装到 `~/.local/opt/intdemo-client`，命令链接位于
`~/.local/bin/intdemo-client`，并创建应用菜单入口。业务数据独立保存在：

```text
~/.local/share/intdemo-client-online-test/
```

首次安装且用户配置不存在时，安装脚本会把随包连接配置和公开根证书复制到
`~/.config/intdemo-client/`；升级时若该配置已经存在则原样保留。

卸载只删除程序，不删除业务数据库：

```bash
~/.local/opt/intdemo-client/uninstall-user.sh
```

## 七、DEB 安装、升级与卸载

首次从用户级安装切换到 DEB 前，必须先删除旧程序和用户级同名桌面入口，避免它
遮盖 UOS 注册的系统入口。该脚本不会删除数据库、在线配置或浏览器账号资料：

```bash
if [[ -x "$HOME/.local/opt/intdemo-client/uninstall-user.sh" ]]; then
  "$HOME/.local/opt/intdemo-client/uninstall-user.sh"
fi
```

随后可在文件管理器中双击当前版本的 `.deb`，或在仓库根目录通过终端安装：

```bash
sudo apt install ./dist/uos-arm64/IntDemo-UOS-arm64-0.2.9.deb
```

同一应用版本内重新构建并测试菜单兼容修复时，使用强制重装让 `dpkg` 刷新文件清单：

```bash
sudo apt install --reinstall ./dist/uos-arm64/IntDemo-UOS-arm64-0.2.9.deb
```

本次生成的是未投递应用商店的测试包；若图形软件包安装器提示签名问题，需要按
UOS 管理策略开启开发者模式，或使用已经获信任签名/企业应用商店发布的包。安装后
从应用菜单启动“逃费车辆信息智能查询平台（UOS）”，并可检查包信息和成品入口：

```bash
dpkg -s com.e23aqiu.intdemo | grep -E '^(Status|Version|Architecture):'
grep -E '^(Name|Exec)=' \
  /opt/apps/com.e23aqiu.intdemo/entries/applications/com.e23aqiu.intdemo.desktop
QT_QPA_PLATFORM=offscreen \
  /opt/apps/com.e23aqiu.intdemo/files/intdemo-client --self-check
```

升级时直接安装更高版本 DEB；卸载只删除程序文件和系统桌面入口：

```bash
sudo apt remove com.e23aqiu.intdemo
```

用户业务数据继续保留在
`~/.local/share/intdemo-client-online-test/`，用户在线配置继续保留在
`~/.config/intdemo-client/`。

根启动器会把菜单入口路径、桌面会话和启动前标准错误追加到以下诊断日志，不记录
账号、令牌或业务内容：

```text
~/.local/share/intdemo-client-online-test/logs/launcher.log
```

## 八、首轮真机验收清单

请按顺序验收，并在某一步失败时停止，保留该步终端与日志：

1. `diagnose.sh` 最终显示“预检通过”。
2. 源码启动后登录页中文字体正常，窗口可拖动、最大化、缩放，无黑边或透明块。
3. 在线登录成功；勾选“记住密码”，关闭并重开后资料可解密；断网时可用有效的
   本机离线授权登录。
4. “系统设置”显示版本并可检查更新；UOS 只接受 `linux-aarch64` 的 `.deb`，
   下载和 SHA-256 校验完成后通过 `pkexec + dpkg`（或系统软件包界面）安装。
5. “运行设置”中的 Chromium 健康检查通过，并显示软件包内
   `browser/chrome` 的真实路径和版本。
6. 用一份脱敏 `.xlsx` 分别跑运输证、营运企业回填、爱企查；检查人工验证码、
   浏览器重启恢复、暂停/继续、停止和原子保存。
7. 导入公开腾讯文档；再测试需要登录的腾讯文档，确认独立浏览器资料目录可复用。
8. 用 WPS 打开测试表格时，应用能提示文件占用；关闭 WPS 后可以继续写入。
9. 关闭应用后检查日志与数据库均位于 XDG 数据目录，重新启动数据仍在。
10. 解压包、用户安装、DEB 安装/应用内升级、应用菜单启动和两种卸载方式各执行一次；
    确认卸载不删除业务数据或在线配置。

## 九、失败时回传的信息

请提供以下文件/输出，注意先检查其中是否含有业务文件名或服务器地址：

```bash
bash scripts/uos-arm64/diagnose.sh 2>&1 | tee uos-arm64-diagnose.log
tail -n 200 ~/.local/share/intdemo-client-online-test/logs/client.log
tail -n 200 ~/.local/share/intdemo-client-online-test/logs/native-crash.log
tail -n 200 ~/.local/share/intdemo-client-online-test/logs/launcher.log
ldd dist/uos-arm64/pyinstaller/intdemo-client/intdemo-client | grep 'not found' || true
grep -ao 'GLIBCXX_[0-9.]*' ./app/_internal/libstdc++.so.6 | sort -Vu | tail -n 1
LD_LIBRARY_PATH="$PWD/app/_internal" ldd ./app/_internal/libQt5Core.so.5
QT_QPA_PLATFORM=offscreen ./intdemo-client --self-check
```

如果浏览器启动失败，再补充：

```bash
find . -path '*/browser/chrome' -type f -print -exec {} --version \;
ldd ./browser/chrome | grep 'not found' || true
echo "$XDG_SESSION_TYPE $DISPLAY $WAYLAND_DISPLAY $QT_QPA_PLATFORM"
```

## 当前限制

- UOS 应用内更新需要系统存在 `pkexec + dpkg` 或可处理 `.deb` 的 `xdg-open`；
  企业策略禁用提权时仍需管理员手动安装已经校验的 DEB。
- Playwright 官方支持的是更新的 Debian/Ubuntu 版本；本项目内置 Chromium
  已在 UOS Desktop 20 1070 ARM64、glibc 2.28 上完成启动验证；游客模式的实际
  业务处理已经通过，在线账号登录、凭据重启解密与同步链路仍需真机验收。
- 当前默认走 XWayland，以降低旧 Qt 5/显卡驱动组合的不确定性；原生 Wayland
  作为后续真机验证项，不作为第一版阻塞条件。
