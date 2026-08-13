#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
env_prefix="${INTDEMO_UOS_ENV_PREFIX:-$repo_root/.conda-uos-arm64}"
browser_cache="${INTDEMO_UOS_BROWSER_CACHE:-$repo_root/.playwright-uos-arm64}"
if [[ "$browser_cache" != /* ]]; then
  browser_cache="$repo_root/$browser_cache"
fi
cd "$repo_root"
skip_tests=0
skip_env_update=0
skip_browser_check=0
skip_deb=0
base_url=""
channel="test"
ca_bundle=""
ca_bundle_mode="default"

usage() {
  cat <<'EOF'
用法：bash scripts/uos-arm64/build.sh [选项]
  --skip-tests          跳过离线自动测试
  --skip-env-update     不更新现有 conda 环境
  --skip-browser-check  只解析浏览器路径，不实际启动 Chromium
  --skip-deb            只生成 tar.gz，不生成 UOS ARM64 DEB
  --base-url URL        写入安装包的 HTTPS 在线服务地址
  --ca-bundle PATH      写入安装包的私有 CA 根证书
  --no-ca-bundle        使用系统公共 CA，不附带私有根证书
  --channel NAME        更新通道：test 或 stable（默认 test）
EOF
}

while (($#)); do
  case "$1" in
    --skip-tests) skip_tests=1 ;;
    --skip-env-update) skip_env_update=1 ;;
    --skip-browser-check) skip_browser_check=1 ;;
    --skip-deb) skip_deb=1 ;;
    --base-url)
      (($# >= 2)) || { echo "错误：--base-url 缺少参数。" >&2; exit 2; }
      base_url="$2"
      shift
      ;;
    --ca-bundle)
      (($# >= 2)) || { echo "错误：--ca-bundle 缺少参数。" >&2; exit 2; }
      ca_bundle="$2"
      ca_bundle_mode="file"
      shift
      ;;
    --no-ca-bundle)
      ca_bundle=""
      ca_bundle_mode="none"
      ;;
    --channel)
      (($# >= 2)) || { echo "错误：--channel 缺少参数。" >&2; exit 2; }
      channel="$2"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ -n "$base_url" && ! "$base_url" =~ ^https://[^[:space:]]+$ ]]; then
  echo "错误：--base-url 必须是 HTTPS URL。" >&2
  exit 2
fi
if [[ "$channel" != "test" && "$channel" != "stable" ]]; then
  echo "错误：--channel 只能是 test 或 stable。" >&2
  exit 2
fi
if [[ "$ca_bundle_mode" == "file" ]]; then
  if [[ "$ca_bundle" != /* ]]; then
    ca_bundle="$repo_root/$ca_bundle"
  fi
  if [[ ! -f "$ca_bundle" ]]; then
    echo "错误：找不到 --ca-bundle 指定的证书：$ca_bundle" >&2
    exit 2
  fi
fi

case "$(uname -m)" in
  aarch64|arm64) ;;
  *) echo "错误：PyInstaller 必须在 UOS ARM64 真机上运行。" >&2; exit 1 ;;
esac

if ((skip_env_update == 0)); then
  bash "$script_dir/prepare-env.sh"
fi

find_conda() {
  if command -v conda >/dev/null 2>&1; then
    command -v conda
    return
  fi
  for candidate in \
    "$HOME/miniforge3/bin/conda" \
    "$HOME/Miniforge3/bin/conda" \
    "$HOME/.local/share/intdemo/miniforge3/bin/conda"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  return 1
}

conda_cmd="$(find_conda)" || {
  echo "错误：未找到 conda，请先运行 prepare-env.sh。" >&2
  exit 1
}
if [[ ! -f "$env_prefix/conda-meta/history" ]]; then
  echo "错误：未找到 $env_prefix，请先运行 prepare-env.sh。" >&2
  exit 1
fi
if [[ ! -d "$browser_cache" ]]; then
  echo "错误：未找到项目内置 Chromium 缓存：$browser_cache" >&2
  echo "请先运行 scripts/uos-arm64/prepare-env.sh。" >&2
  exit 1
fi

version="$("$conda_cmd" run --prefix "$env_prefix" python -c 'from integrated_client.config import APP_VERSION; print(APP_VERSION)')"
version="$(printf '%s' "$version" | tr -d '\r' | tail -n 1)"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误：无法识别客户端版本：$version" >&2
  exit 1
fi
package_parent="$repo_root/dist/uos-arm64/package"
package_name="IntDemo-UOS-arm64-$version"
package_root="$package_parent/$package_name"
artifact="$repo_root/dist/uos-arm64/$package_name.tar.gz"
deb_artifact="$repo_root/dist/uos-arm64/$package_name.deb"

case "$package_root" in
  "$repo_root"/dist/uos-arm64/package/*) ;;
  *) echo "拒绝清理非预期打包目录：$package_root" >&2; exit 1 ;;
esac
case "$artifact" in
  "$repo_root"/dist/uos-arm64/IntDemo-UOS-arm64-*.tar.gz) ;;
  *) echo "拒绝清理非预期构建产物：$artifact" >&2; exit 1 ;;
esac
case "$deb_artifact" in
  "$repo_root"/dist/uos-arm64/IntDemo-UOS-arm64-*.deb) ;;
  *) echo "拒绝清理非预期 DEB 产物：$deb_artifact" >&2; exit 1 ;;
esac
echo "=== 清理同版本旧构建产物 ==="
rm -rf -- "$package_root"
rm -f -- "$artifact" "$artifact.sha256"
rm -f -- "$deb_artifact" "$deb_artifact.sha256"

browser_output="$(
  PLAYWRIGHT_BROWSERS_PATH="$browser_cache" \
    "$conda_cmd" run --prefix "$env_prefix" python -c \
    'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print("INTDEMO_BROWSER_PATH=" + p.chromium.executable_path); p.stop()'
)"
browser_path="$(
  printf '%s\n' "$browser_output" | \
    sed -n 's/^INTDEMO_BROWSER_PATH=//p' | tail -n 1
)"
if [[ -z "$browser_path" || ! -x "$browser_path" ]]; then
  echo "错误：项目缓存中没有可执行的 Playwright Chromium。" >&2
  echo "$browser_output" >&2
  exit 1
fi
browser_cache_real="$(readlink -f "$browser_cache")"
browser_path="$(readlink -f "$browser_path")"
case "$browser_path" in
  "$browser_cache_real"/*) ;;
  *) echo "拒绝打包项目缓存之外的浏览器：$browser_path" >&2; exit 1 ;;
esac
if [[ "$(basename "$browser_path")" != "chrome" ]]; then
  echo "错误：无法识别 Chromium 可执行文件名：$browser_path" >&2
  exit 1
fi
browser_source_dir="$(dirname "$browser_path")"
export PLAYWRIGHT_BROWSERS_PATH="$browser_cache_real"

preflight_args=(--require-uos)
if ((skip_browser_check)); then
  preflight_args+=(--skip-browser-launch)
fi
INTDEMO_CHROMIUM_PATH="$browser_path" \
  "$conda_cmd" run --prefix "$env_prefix" \
  python "$script_dir/preflight.py" "${preflight_args[@]}"

if ((skip_tests == 0)); then
  echo "=== 运行离线回归测试 ==="
  QT_QPA_PLATFORM=offscreen "$conda_cmd" run --prefix "$env_prefix" \
    python -m unittest discover -s "$repo_root/tests" -v
fi

echo "=== 构建 PyInstaller ARM64 目录包 ==="
pyinstaller_dist="$repo_root/dist/uos-arm64/pyinstaller"
pyinstaller_work="$repo_root/build/uos-arm64"
mkdir -p "$pyinstaller_dist" "$pyinstaller_work"
"$conda_cmd" run --prefix "$env_prefix" \
  pyinstaller --noconfirm --clean \
  --distpath "$pyinstaller_dist" \
  --workpath "$pyinstaller_work" \
  "$repo_root/integrated_client_uos_arm64.spec"

echo "=== 固定打包 ARM64 GNU 运行库 ==="
pyinstaller_app="$pyinstaller_dist/intdemo-client"
pyinstaller_internal="$pyinstaller_app/_internal"
case "$pyinstaller_internal" in
  "$repo_root"/dist/uos-arm64/pyinstaller/intdemo-client/_internal) ;;
  *) echo "拒绝修改非预期 PyInstaller 目录：$pyinstaller_internal" >&2; exit 1 ;;
esac
if [[ ! -d "$pyinstaller_internal" ]]; then
  echo "错误：未找到 PyInstaller 运行库目录：$pyinstaller_internal" >&2
  exit 1
fi

for runtime_library in libstdc++.so.6 libgcc_s.so.1; do
  runtime_source="$env_prefix/lib/$runtime_library"
  if [[ ! -f "$runtime_source" ]]; then
    echo "错误：conda 环境缺少 $runtime_library，请重新运行 prepare-env.sh。" >&2
    exit 1
  fi
  rm -f -- "$pyinstaller_internal/$runtime_library"
  cp -L -- "$runtime_source" "$pyinstaller_internal/$runtime_library"
  chmod 0644 "$pyinstaller_internal/$runtime_library"
done

required_glibcxx="GLIBCXX_3.4.26"
bundled_libstdcxx="$pyinstaller_internal/libstdc++.so.6"
if ! LC_ALL=C grep -aFq "$required_glibcxx" "$bundled_libstdcxx"; then
  echo "错误：包内 libstdc++.so.6 不提供 Qt 所需的 $required_glibcxx。" >&2
  exit 1
fi
bundled_glibcxx_max="$(
  LC_ALL=C grep -ao 'GLIBCXX_[0-9][0-9.]*' "$bundled_libstdcxx" | \
    sort -Vu | tail -n 1
)"
echo "包内 C++ ABI: $bundled_glibcxx_max（最低要求 $required_glibcxx）"

qt_core="$pyinstaller_internal/libQt5Core.so.5"
if [[ ! -f "$qt_core" ]]; then
  echo "错误：未找到包内 Qt Core：$qt_core" >&2
  exit 1
fi
qt_ldd_output="$(
  LD_LIBRARY_PATH="$pyinstaller_internal${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    ldd "$qt_core" 2>&1 || true
)"
if grep -Fqi "not found" <<<"$qt_ldd_output"; then
  echo "错误：包内 Qt Core 存在缺失动态库：" >&2
  echo "$qt_ldd_output" >&2
  exit 1
fi
qt_libstdcxx_path="$(
  sed -n \
    's/^[[:space:]]*libstdc++\.so\.6 => \([^[:space:]]*\).*/\1/p' \
    <<<"$qt_ldd_output" | head -n 1
)"
if [[ -z "$qt_libstdcxx_path" || ! -e "$qt_libstdcxx_path" ]]; then
  echo "错误：Qt Core 未解析到包内 libstdc++.so.6：" >&2
  echo "$qt_ldd_output" >&2
  exit 1
fi
qt_libstdcxx_real="$(readlink -f "$qt_libstdcxx_path")"
bundled_libstdcxx_real="$(readlink -f "$bundled_libstdcxx")"
if [[ "$qt_libstdcxx_real" != "$bundled_libstdcxx_real" ]]; then
  echo "错误：Qt Core 使用了包外 libstdc++.so.6：$qt_libstdcxx_real" >&2
  echo "$qt_ldd_output" >&2
  exit 1
fi
echo "Qt Core C++ 运行库: $qt_libstdcxx_real"
echo "Qt Core 动态库检查: 正常"

fcitx_input_plugin="$(
  find /usr/lib -path '*/qt5/plugins/platforminputcontexts/*' \
    -name 'libfcitx*inputcontextplugin.so*' -print -quit 2>/dev/null || true
)"
if [[ -z "$fcitx_input_plugin" ]]; then
  echo "错误：UOS 系统缺少 Qt Fcitx 输入法插件。" >&2
  echo "请确认系统已安装 fcitx-frontend-qt5。" >&2
  exit 1
fi
input_plugin_ldd="$(ldd "$fcitx_input_plugin" 2>&1 || true)"
if grep -Fqi "not found" <<<"$input_plugin_ldd"; then
  echo "错误：Qt Fcitx 输入法插件存在缺失动态库：" >&2
  echo "$input_plugin_ldd" >&2
  exit 1
fi
echo "Qt 中文输入法系统插件: $fcitx_input_plugin"

# Never add the distro Qt plugin root to QT_PLUGIN_PATH.  UOS ships Qt 5.11,
# while the conda runtime bundles Qt 5.15; exposing the whole distro tree lets
# image format, platform theme and style plugins cross that ABI boundary and
# can segfault inside QApplication.  Copy only the required input context into
# the already-isolated PyInstaller Qt plugin tree.
qt_plugin_root="$pyinstaller_internal/PyQt5/Qt5/plugins"
if [[ ! -d "$qt_plugin_root" ]]; then
  echo "错误：未找到包内 Qt 插件目录：$qt_plugin_root" >&2
  exit 1
fi
bundled_input_context_dir="$qt_plugin_root/platforminputcontexts"
mkdir -p "$bundled_input_context_dir"
bundled_fcitx_input_plugin="$bundled_input_context_dir/$(basename "$fcitx_input_plugin")"
cp -L -- "$fcitx_input_plugin" "$bundled_fcitx_input_plugin"
chmod 0644 "$bundled_fcitx_input_plugin"

bundled_input_plugin_ldd="$(
  LD_LIBRARY_PATH="$pyinstaller_internal${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    ldd "$bundled_fcitx_input_plugin" 2>&1 || true
)"
if grep -Fqi "not found" <<<"$bundled_input_plugin_ldd"; then
  echo "错误：私有 Qt Fcitx 输入法插件存在缺失动态库：" >&2
  echo "$bundled_input_plugin_ldd" >&2
  exit 1
fi
bundled_qt_dependency_count=0
while IFS= read -r resolved_qt_path; do
  [[ -n "$resolved_qt_path" ]] || continue
  resolved_qt_path="$(readlink -f "$resolved_qt_path")"
  case "$resolved_qt_path" in
    "$pyinstaller_internal"/*) ;;
    *)
      echo "错误：Fcitx 输入法插件解析到了包外 Qt：$resolved_qt_path" >&2
      echo "$bundled_input_plugin_ldd" >&2
      exit 1
      ;;
  esac
  ((bundled_qt_dependency_count += 1))
done < <(
  sed -n \
    's/^[[:space:]]*libQt5[^[:space:]]* => \([^[:space:]]*\).*/\1/p' \
    <<<"$bundled_input_plugin_ldd"
)
if ((bundled_qt_dependency_count == 0)); then
  echo "错误：无法确认 Fcitx 输入法插件使用包内 Qt。" >&2
  echo "$bundled_input_plugin_ldd" >&2
  exit 1
fi
echo "Qt 中文输入法私有插件: $bundled_fcitx_input_plugin"

mkdir -p "$package_root/app" "$package_root/browser" "$package_root/certs"
cp -a "$pyinstaller_dist/intdemo-client/." "$package_root/app/"
cp -a "$browser_source_dir/." "$package_root/browser/"
cp "$repo_root/packaging/uos-arm64/client-online.json" "$package_root/"
package_ca_bundle=""
if [[ "$ca_bundle_mode" == "default" ]]; then
  ca_bundle="$repo_root/packaging/uos-arm64/certs/intdemo-caddy-root.crt"
fi
if [[ "$ca_bundle_mode" != "none" ]]; then
  cp "$ca_bundle" "$package_root/certs/intdemo-caddy-root.crt"
  package_ca_bundle="certs/intdemo-caddy-root.crt"
fi
cp "$repo_root/packaging/uos-arm64/intdemo-client" "$package_root/"
cp "$repo_root/packaging/uos-arm64/install-user.sh" "$package_root/"
cp "$repo_root/packaging/uos-arm64/uninstall-user.sh" "$package_root/"
cp "$repo_root/packaging/uos-arm64/com.e23aqiu.intdemo.desktop" "$package_root/"
cp "$repo_root/packaging/uos-arm64/README.txt" "$package_root/"
cp "$repo_root/docs/UOS_ARM64.md" "$package_root/"
cp "$repo_root/integrated_client/ui/assets/app-icon.png" "$package_root/"
INTDEMO_PACKAGE_BASE_URL="$base_url" \
INTDEMO_PACKAGE_CHANNEL="$channel" \
INTDEMO_PACKAGE_CA_BUNDLE="$package_ca_bundle" \
  "$conda_cmd" run --prefix "$env_prefix" python -c \
  'import json, os, pathlib; p=pathlib.Path("'"$package_root"'/client-online.json"); data=json.loads(p.read_text(encoding="utf-8")); url=os.environ["INTDEMO_PACKAGE_BASE_URL"].strip(); data["base_url"]=(url.rstrip("/") if url else data["base_url"]); data["channel"]=os.environ["INTDEMO_PACKAGE_CHANNEL"]; data["ca_bundle"]=os.environ["INTDEMO_PACKAGE_CA_BUNDLE"] or None; p.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")'
chmod +x \
  "$package_root/intdemo-client" \
  "$package_root/install-user.sh" \
  "$package_root/uninstall-user.sh" \
  "$package_root/app/intdemo-client" \
  "$package_root/browser/chrome"

echo "=== 检查打包后的在线配置 ==="
INTDEMO_CONNECTION_CONFIG="$package_root/client-online.json" \
  "$conda_cmd" run --prefix "$env_prefix" python -c \
  'from integrated_client.online.config import OnlineConfig; config = OnlineConfig.load(); print("在线配置正常:", config.base_url, config.ca_bundle)'

echo "=== 检查打包后的客户端运行库与 Qt 插件隔离 ==="
qt_plugin_debug_log="$pyinstaller_work/qt-plugin-self-check.log"
rm -f -- "$qt_plugin_debug_log"
QT_QPA_PLATFORM=offscreen \
QT_IM_MODULE=fcitx \
QT_DEBUG_PLUGINS=1 \
  "$package_root/app/intdemo-client" --self-check \
  2>"$qt_plugin_debug_log"
package_fcitx_input_plugin="$package_root/app/_internal/PyQt5/Qt5/plugins/platforminputcontexts/$(basename "$fcitx_input_plugin")"
if grep -Eq \
  '"/(usr/(local/)?lib(64)?|lib(64)?)/[^" ]*qt5/plugins/' \
  "$qt_plugin_debug_log"; then
  echo "错误：打包客户端仍在发现 UOS 系统 Qt 插件：" >&2
  grep -E \
    '"/(usr/(local/)?lib(64)?|lib(64)?)/[^" ]*qt5/plugins/' \
    "$qt_plugin_debug_log" | tail -n 30 >&2
  exit 1
fi
if ! grep -Fq \
  "loaded library \"$package_fcitx_input_plugin\"" \
  "$qt_plugin_debug_log"; then
  echo "错误：打包客户端未加载私有 Fcitx 输入法插件。" >&2
  tail -n 100 "$qt_plugin_debug_log" >&2
  exit 1
fi
echo "Qt 插件隔离检查: 正常"

if [[ -n "${DISPLAY:-}" ]]; then
  echo "=== 检查打包客户端的 XCB 图形初始化 ==="
  INTDEMO_QT_QPA_PLATFORM=xcb \
    QT_IM_MODULE=compose \
    "$package_root/intdemo-client" --self-check
  INTDEMO_QT_QPA_PLATFORM=xcb \
    QT_IM_MODULE=fcitx \
    "$package_root/intdemo-client" --self-check
else
  echo "提示：当前构建会话没有 DISPLAY，已跳过 XCB 真机启动检查。"
fi

if ((skip_browser_check == 0)); then
  echo "=== 检查打包后的内置 Chromium ==="
  INTDEMO_CHROMIUM_PATH="$package_root/browser/chrome" \
    "$conda_cmd" run --prefix "$env_prefix" python -c \
    'from integrated_client.browser import check_builtin_chromium; path, version = check_builtin_chromium(); print("打包浏览器正常:", version, path)'
fi

{
  printf 'version=%s\n' "$version"
  printf 'git_commit=%s\n' "$(git -C "$repo_root" rev-parse HEAD)"
  printf 'built_at=%s\n' "$(date --iso-8601=seconds)"
  printf 'os=%s\n' "$(grep -m1 '^SystemName=' /etc/os-version 2>/dev/null || true)"
  printf 'architecture=%s\n' "$(uname -m)"
  printf 'glibc=%s\n' "$(ldd --version 2>&1 | head -n 1)"
  printf 'libstdcxx=%s\n' "$bundled_glibcxx_max"
  printf 'chromium=%s\n' "$("$browser_path" --version 2>&1 | head -n 1)"
} > "$package_root/build-info.txt"

mkdir -p "$package_parent"
tar -C "$package_parent" -czf "$artifact" "$package_name"
sha256sum "$artifact" > "$artifact.sha256"

if ((skip_deb == 0)); then
  bash "$script_dir/build-deb.sh" \
    --package-root "$package_root" \
    --version "$version"
fi

echo "=== 构建完成 ==="
echo "$artifact"
cat "$artifact.sha256"
size_targets=("$package_root/browser" "$artifact")
if [[ -f "$deb_artifact" ]]; then
  size_targets+=("$deb_artifact")
fi
du -sh "${size_targets[@]}"
echo "解压后先运行 ./intdemo-client；确认无误后可运行 ./install-user.sh。"
