#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
python_cmd="${INTDEMO_UOS_PYTHON:-$repo_root/.conda-uos-arm64/bin/python}"

if [[ ! -x "$python_cmd" ]]; then
  python_cmd="$(command -v python3 || true)"
fi
if [[ -z "$python_cmd" || ! -x "$python_cmd" ]]; then
  echo "错误：未找到 UOS 逐文件更新构建所需的 Python。" >&2
  exit 2
fi
if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "错误：未找到 dpkg-deb，无法读取 UOS 基线包。" >&2
  exit 2
fi

exec "$python_cmd" "$script_dir/build_files.py" "$@"
