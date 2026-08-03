#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
env_prefix="${INTDEMO_UOS_ENV_PREFIX:-$repo_root/.conda-uos-arm64}"
cd "$repo_root"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "错误：UOS 构建环境必须是 Linux。" >&2
  exit 1
fi
case "$(uname -m)" in
  aarch64|arm64) ;;
  *)
    echo "错误：必须在 ARM64 真机上准备环境，当前架构为 $(uname -m)。" >&2
    exit 1
    ;;
esac

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

if ! conda_cmd="$(find_conda)"; then
  cat >&2 <<'EOF'
错误：未找到 Miniforge/conda。
请先安装 ARM64 Miniforge（不会替换 UOS 的系统 Python）：
  wget -O Miniforge3-Linux-aarch64.sh \
    https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
  bash Miniforge3-Linux-aarch64.sh
安装后重新打开终端，再运行本脚本。
EOF
  exit 1
fi

echo "使用 conda: $conda_cmd"
echo "隔离环境: $env_prefix"
if [[ -f "$env_prefix/conda-meta/history" ]]; then
  "$conda_cmd" env update \
    --prefix "$env_prefix" \
    --file "$repo_root/environment-uos-arm64.yml" \
    --prune
else
  "$conda_cmd" env create \
    --prefix "$env_prefix" \
    --file "$repo_root/environment-uos-arm64.yml"
fi

"$conda_cmd" run --prefix "$env_prefix" \
  python -m pip install --upgrade \
  --requirement "$repo_root/requirements-uos-arm64.txt"

"$conda_cmd" run --prefix "$env_prefix" \
  python -c 'import platform, PyQt5.QtCore; print("环境就绪:", platform.python_version(), "Qt", PyQt5.QtCore.QT_VERSION_STR)'

echo "UOS ARM64 构建环境已准备完成。"
