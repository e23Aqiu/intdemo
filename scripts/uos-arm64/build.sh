#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
env_prefix="${INTDEMO_UOS_ENV_PREFIX:-$repo_root/.conda-uos-arm64}"
cd "$repo_root"
skip_tests=0
skip_env_update=0
skip_browser_check=0

usage() {
  cat <<'EOF'
用法：bash scripts/uos-arm64/build.sh [选项]
  --skip-tests          跳过离线自动测试
  --skip-env-update     不更新现有 conda 环境
  --skip-browser-check  只解析浏览器路径，不实际启动 Chromium
EOF
}

while (($#)); do
  case "$1" in
    --skip-tests) skip_tests=1 ;;
    --skip-env-update) skip_env_update=1 ;;
    --skip-browser-check) skip_browser_check=1 ;;
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

preflight_args=(--require-uos)
if ((skip_browser_check)); then
  preflight_args+=(--skip-browser-launch)
fi
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

version="$("$conda_cmd" run --prefix "$env_prefix" python -c 'from integrated_client.config import APP_VERSION; print(APP_VERSION)')"
version="$(printf '%s' "$version" | tr -d '\r' | tail -n 1)"
package_parent="$repo_root/dist/uos-arm64/package"
package_name="IntDemo-UOS-arm64-$version"
package_root="$package_parent/$package_name"
artifact="$repo_root/dist/uos-arm64/$package_name.tar.gz"

case "$package_root" in
  "$repo_root"/dist/uos-arm64/package/*) ;;
  *) echo "拒绝清理非预期打包目录：$package_root" >&2; exit 1 ;;
esac
rm -rf -- "$package_root"
rm -f -- "$artifact" "$artifact.sha256"
mkdir -p "$package_root/app"
cp -a "$pyinstaller_dist/intdemo-client/." "$package_root/app/"
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
  "$package_root/app/intdemo-client"

{
  printf 'version=%s\n' "$version"
  printf 'git_commit=%s\n' "$(git -C "$repo_root" rev-parse HEAD)"
  printf 'built_at=%s\n' "$(date --iso-8601=seconds)"
  printf 'os=%s\n' "$(grep -m1 '^SystemName=' /etc/os-version 2>/dev/null || true)"
  printf 'architecture=%s\n' "$(uname -m)"
  printf 'glibc=%s\n' "$(ldd --version 2>&1 | head -n 1)"
} > "$package_root/build-info.txt"

mkdir -p "$package_parent"
tar -C "$package_parent" -czf "$artifact" "$package_name"
sha256sum "$artifact" > "$artifact.sha256"

echo "=== 构建完成 ==="
echo "$artifact"
cat "$artifact.sha256"
echo "解压后先运行 ./intdemo-client；确认无误后可运行 ./install-user.sh。"
