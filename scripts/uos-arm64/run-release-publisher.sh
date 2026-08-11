#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
env_prefix="${INTDEMO_UOS_ENV_PREFIX:-$repo_root/.conda-uos-arm64}"
python="$env_prefix/bin/python"

if [[ ! -x "$python" ]]; then
  echo "错误：未找到 UOS 项目环境：$python" >&2
  echo "请先运行：bash scripts/uos-arm64/prepare-env.sh" >&2
  exit 1
fi
if [[ -z "${DISPLAY:-}" ]]; then
  echo "错误：请从 UOS 图形桌面的终端启动打包发布器。" >&2
  exit 1
fi
if ! command -v zenity >/dev/null 2>&1; then
  echo "提示：未安装 zenity，文件选择会暂时回退到 Qt 窗口。" >&2
  echo "可执行 sudo apt install zenity 启用 UOS 系统文件选择窗口。" >&2
fi

cd "$repo_root"
exec "$python" -m release_publisher
