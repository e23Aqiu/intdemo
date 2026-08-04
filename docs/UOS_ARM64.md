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
- UOS 包必须在这台 ARM64 真机上构建。PyInstaller 不是交叉编译器；同时在
  glibc 2.28 上构建可以避免产物误依赖更新的 glibc。

## 一、准备系统依赖

先确认当前用户可以使用 `sudo`，然后安装基础工具和常见桌面运行库：

```bash
sudo apt update
sudo apt install -y \
  wget ca-certificates git binutils lsof libsecret-tools \
  libglib2.0-0 libdbus-1-3 libfontconfig1 libfreetype6 \
  libx11-6 libx11-xcb1 libxcb1 libxcb-xinerama0 libxkbcommon-x11-0 \
  libxrender1 libxi6 libxrandr2 libxfixes3 libxcursor1 \
  libgl1 libegl1 libgbm1 libnss3 libasound2 fonts-noto-cjk
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
git switch codex/uos-arm64-compat
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

在线模式把连接配置放在用户配置目录，安装或升级不会覆盖：

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/intdemo-client"
cat > "${XDG_CONFIG_HOME:-$HOME/.config}/intdemo-client/client-online.json" <<'JSON'
{
  "base_url": "https://api.example.com",
  "ca_bundle": null,
  "channel": "test",
  "connect_timeout": 5,
  "read_timeout": 20
}
JSON
```

如果使用公网 IP 和自建 Caddy CA，必须把 `ca_bundle` 改成该根证书的绝对
路径；客户端不会允许 `http://` 或 `verify=False` 式绕过。

Wayland 会话且存在 XWayland 时，程序默认使用 `QT_QPA_PLATFORM=xcb` 和
Chromium `--ozone-platform=x11`。如果真机已经安装完整 Qt Wayland 插件并想
验证原生 Wayland，可显式覆盖：

```bash
export QT_QPA_PLATFORM=wayland
export INTDEMO_CHROMIUM_OZONE_PLATFORM=wayland
conda run -p ./.conda-uos-arm64 python main.py
```

## 五、构建 ARM64 包

```bash
bash scripts/uos-arm64/build.sh
```

默认流程依次执行：环境和浏览器缓存更新、UOS/架构/glibc/依赖/浏览器/密钥环
预检、客户端离线回归测试、PyInstaller 目录包构建、conda ARM64 GNU 运行库
固定、Qt 所需 `GLIBCXX_3.4.26` 与动态库检查、成品入口自检、内置 Chromium
复制与二次启动检查、`tar.gz` 打包和 SHA-256 生成。构建脚本拒绝打包项目缓存
目录之外的浏览器，避免误带入开发机上的其他可执行文件。

仅在定位问题时使用跳过参数：

```bash
bash scripts/uos-arm64/build.sh --skip-env-update --skip-tests
bash scripts/uos-arm64/build.sh --skip-browser-check
```

成功后得到：

```text
dist/uos-arm64/IntDemo-UOS-arm64-<版本>.tar.gz
dist/uos-arm64/IntDemo-UOS-arm64-<版本>.tar.gz.sha256
```

压缩包内的 `browser/` 是完整 Chromium 运行目录；启动器会自动设置
`INTDEMO_CHROMIUM_PATH`，最终用户不需要执行 `playwright install`。
`app/_internal/libstdc++.so.6` 和 `libgcc_s.so.1` 来自项目 conda 环境，避免
PyInstaller 误收集 UOS 系统旧库后无法加载 conda-forge Qt。

## 六、试运行与当前用户安装

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

卸载只删除程序，不删除业务数据库：

```bash
~/.local/opt/intdemo-client/uninstall-user.sh
```

## 七、首轮真机验收清单

请按顺序验收，并在某一步失败时停止，保留该步终端与日志：

1. `diagnose.sh` 最终显示“预检通过”。
2. 源码启动后登录页中文字体正常，窗口可拖动、最大化、缩放，无黑边或透明块。
3. 在线登录成功；勾选“记住密码”，关闭并重开后资料可解密；断网时可用有效的
   本机离线授权登录。
4. “系统设置”显示版本；UOS 暂不展示 Windows `.exe` 自动更新入口。
5. “运行设置”中的 Chromium 健康检查通过，并显示软件包内
   `browser/chrome` 的真实路径和版本。
6. 用一份脱敏 `.xlsx` 分别跑运输证、营运企业回填、爱企查；检查人工验证码、
   浏览器重启恢复、暂停/继续、停止和原子保存。
7. 导入公开腾讯文档；再测试需要登录的腾讯文档，确认独立浏览器资料目录可复用。
8. 用 WPS 打开测试表格时，应用能提示文件占用；关闭 WPS 后可以继续写入。
9. 关闭应用后检查日志与数据库均位于 XDG 数据目录，重新启动数据仍在。
10. 解压包、用户安装、应用菜单启动和卸载各执行一次；确认卸载不删除业务数据。

## 八、失败时回传的信息

请提供以下文件/输出，注意先检查其中是否含有业务文件名或服务器地址：

```bash
bash scripts/uos-arm64/diagnose.sh 2>&1 | tee uos-arm64-diagnose.log
tail -n 200 ~/.local/share/intdemo-client-online-test/logs/client.log
tail -n 200 ~/.local/share/intdemo-client-online-test/logs/native-crash.log
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

- UOS 自动更新暂时关闭，因为现有更新清单和发布器只生成 Windows `.exe`。
  UOS 测试包通过重新构建后执行 `install-user.sh` 覆盖升级。
- Playwright 官方支持的是更新的 Debian/Ubuntu 版本；本项目内置 Chromium
  已在 UOS Desktop 20 1070 ARM64、glibc 2.28 上完成首次启动验证，但仍需通过
  真实运输证、营运查询、爱企查和腾讯文档业务流程验收。
- 当前默认走 XWayland，以降低旧 Qt 5/显卡驱动组合的不确定性；原生 Wayland
  作为后续真机验证项，不作为第一版阻塞条件。
