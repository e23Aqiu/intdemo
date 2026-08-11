#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
env_prefix="${INTDEMO_UOS_ENV_PREFIX:-$repo_root/.conda-uos-arm64}"
browser_cache="${INTDEMO_UOS_BROWSER_CACHE:-$repo_root/.playwright-uos-arm64}"
if [[ "$browser_cache" != /* ]]; then
  browser_cache="$repo_root/$browser_cache"
fi
if [[ -d "$browser_cache" ]]; then
  export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$browser_cache}"
fi
cd "$repo_root"

echo "=== 系统信息 ==="
if [[ -f /etc/os-version ]]; then
  cat /etc/os-version
elif [[ -f /etc/os-release ]]; then
  cat /etc/os-release
fi
uname -a
ldd --version 2>&1 | head -n 1
printf 'XDG_SESSION_TYPE=%s\n' "${XDG_SESSION_TYPE:-}"
printf 'DISPLAY=%s\n' "${DISPLAY:-}"
printf 'WAYLAND_DISPLAY=%s\n' "${WAYLAND_DISPLAY:-}"
printf 'QT_QPA_PLATFORM=%s\n' "${QT_QPA_PLATFORM:-}"
printf 'INTDEMO_CHROMIUM_PATH=%s\n' "${INTDEMO_CHROMIUM_PATH:-}"
printf 'PLAYWRIGHT_BROWSERS_PATH=%s\n' "${PLAYWRIGHT_BROWSERS_PATH:-}"

echo
echo "=== 浏览器候选 ==="
for command in chromium chromium-browser uos-browser uos-browser-stable deepin-browser deepin-browser-stable google-chrome-stable google-chrome; do
  if path="$(command -v "$command" 2>/dev/null)"; then
    printf '%-24s %s\n' "$command" "$path"
    "$path" --version 2>&1 | head -n 1 || true
  fi
done
command -v secret-tool || true

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

echo
echo "=== 项目预检 ==="
if ! conda_cmd="$(find_conda)"; then
  echo "未找到 conda，请先运行 scripts/uos-arm64/prepare-env.sh。" >&2
  exit 1
fi
if [[ ! -f "$env_prefix/conda-meta/history" ]]; then
  echo "未找到项目环境，请先运行 scripts/uos-arm64/prepare-env.sh。" >&2
  exit 1
fi
"$conda_cmd" run --prefix "$env_prefix" \
  python "$script_dir/preflight.py" --require-uos
