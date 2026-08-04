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

usage() {
  cat <<'EOF'
用法：bash scripts/uos-arm64/build.sh [选项]
  --skip-tests          跳过离线自动测试
  --skip-env-update     不更新现有 conda 环境
  --skip-browser-check  只解析浏览器路径，不实际启动 Chromium
  --skip-deb            只生成 tar.gz，不生成 UOS ARM64 DEB
EOF
}

while (($#)); do
  case "$1" in
    --skip-tests) skip_tests=1 ;;
    --skip-env-update) skip_env_update=1 ;;
    --skip-browser-check) skip_browser_check=1 ;;
    --skip-deb) skip_deb=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

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

mkdir -p "$package_root/app" "$package_root/browser" "$package_root/certs"
cp -a "$pyinstaller_dist/intdemo-client/." "$package_root/app/"
cp -a "$browser_source_dir/." "$package_root/browser/"
cp "$repo_root/packaging/uos-arm64/client-online.json" "$package_root/"
cp "$repo_root/packaging/uos-arm64/certs/intdemo-caddy-root.crt" \
  "$package_root/certs/"
cp "$repo_root/packaging/uos-arm64/intdemo-client" "$package_root/"
cp "$repo_root/packaging/uos-arm64/install-user.sh" "$package_root/"
cp "$repo_root/packaging/uos-arm64/uninstall-user.sh" "$package_root/"
cp "$repo_root/packaging/uos-arm64/com.e23aqiu.intdemo.desktop" "$package_root/"
cp "$repo_root/packaging/uos-arm64/README.txt" "$package_root/"
cp "$repo_root/docs/UOS_ARM64.md" "$package_root/"
cp "$repo_root/integrated_client/ui/assets/app-icon.png" "$package_root/"
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

echo "=== 检查打包后的客户端运行库 ==="
QT_QPA_PLATFORM=offscreen "$package_root/intdemo-client" --self-check

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
