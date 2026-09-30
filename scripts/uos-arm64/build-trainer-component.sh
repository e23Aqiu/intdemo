#!/usr/bin/env bash
set -Eeuo pipefail

# Build only the local CAPTCHA trainer component. This script intentionally
# does not call the UOS client, tar.gz, DEB, update, or server build pipelines.

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
cd "$repo_root"

wheelhouse="${INTDEMO_TRAINER_WHEELHOUSE:-}"
python_path="${INTDEMO_TRAINER_PYTHON:-}"
env_prefix="${INTDEMO_TRAINER_ENV_PREFIX:-}"
output_dir=""
build_root="${INTDEMO_TRAINER_BUILD_ROOT:-}"

usage() {
  cat <<'EOF'
用法：bash scripts/uos-arm64/build-trainer-component.sh [选项]

只构建管理员本机使用的验证码强化训练组件 .inttrainer，不构建 UOS 客户端、
tar.gz、DEB 或服务端。普通业务客户端不需要安装该组件。

选项：
  --wheelhouse PATH   已审核的 Linux ARM64 离线 wheel 目录；首次运行必需
  --python PATH       使用已准备好的 Python 3.10 ARM64 环境
  --env-prefix PATH   指定脚本管理的 conda 环境目录
  --output-dir PATH   指定组件输出目录
  --build-root PATH   指定 PyInstaller 临时构建目录
  -h, --help          显示帮助

也可使用环境变量：
  INTDEMO_TRAINER_WHEELHOUSE
  INTDEMO_TRAINER_PYTHON
  INTDEMO_TRAINER_ENV_PREFIX
  INTDEMO_TRAINER_BUILD_ROOT
EOF
}

while (($#)); do
  case "$1" in
    --wheelhouse|--python|--env-prefix|--output-dir|--build-root)
      (($# >= 2)) || { echo "错误：$1 缺少参数。" >&2; exit 2; }
      option="$1"
      value="$2"
      shift
      case "$option" in
        --wheelhouse) wheelhouse="$value" ;;
        --python) python_path="$value" ;;
        --env-prefix) env_prefix="$value" ;;
        --output-dir) output_dir="$value" ;;
        --build-root) build_root="$value" ;;
      esac
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "错误：未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "错误：该脚本只能在 UOS/Linux ARM64 上运行。" >&2
  exit 1
fi
case "$(uname -m)" in
  aarch64|arm64) ;;
  *)
    echo "错误：组件必须在 ARM64 真机上构建，当前架构为 $(uname -m)。" >&2
    exit 1
    ;;
esac

component_version="$({
  sed -n 's/^COMPONENT_VERSION = "\([0-9][0-9.]*\)"[[:space:]]*$/\1/p' \
    "$repo_root/enhanced_trainer/protocol.py"
} | head -n 1)"
if [[ ! "$component_version" =~ ^[0-9]+(\.[0-9]+){2,3}$ ]]; then
  echo "错误：无法从 enhanced_trainer/protocol.py 读取组件版本。" >&2
  exit 1
fi

make_absolute() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$repo_root" "$1" ;;
  esac
}

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

if [[ -n "$wheelhouse" ]]; then
  wheelhouse="$(make_absolute "$wheelhouse")"
  if [[ ! -d "$wheelhouse" ]]; then
    echo "错误：离线 wheel 目录不存在：$wheelhouse" >&2
    exit 1
  fi
fi

if [[ -n "$python_path" ]]; then
  requested_python="$python_path"
  if [[ "$python_path" == */* ]]; then
    python_path="$(make_absolute "$python_path")"
  else
    python_path="$(command -v "$python_path" || true)"
  fi
  if [[ ! -x "$python_path" ]]; then
    echo "错误：找不到可执行的 --python：$requested_python" >&2
    exit 1
  fi
else
  if [[ -z "$env_prefix" ]]; then
    data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
    env_prefix="$data_home/intdemo-builder/trainer-$component_version-py310"
  else
    env_prefix="$(make_absolute "$env_prefix")"
  fi
  python_path="$env_prefix/bin/python"
  if [[ ! -x "$python_path" ]]; then
    if [[ -e "$env_prefix" ]]; then
      echo "错误：环境目录已存在但不是有效的 Python 环境：$env_prefix" >&2
      exit 1
    fi
    if [[ -z "$wheelhouse" ]]; then
      cat >&2 <<EOF
错误：首次运行需要 --wheelhouse 指定已审核的 Linux ARM64 离线 wheel 目录。
以后会复用独立环境：$env_prefix
EOF
      exit 1
    fi
    conda_cmd="$(find_conda)" || {
      echo "错误：未找到 ARM64 Miniforge/conda，无法创建 Python 3.10 独立环境。" >&2
      exit 1
    }
    mkdir -p "$(dirname "$env_prefix")"
    echo "=== 创建组件构建环境 ==="
    echo "$env_prefix"
    "$conda_cmd" create --yes --prefix "$env_prefix" python=3.10 pip
  fi
fi

"$python_path" - <<'PY'
import platform
import struct
import sys

if sys.version_info[:2] != (3, 10):
    raise SystemExit(f"需要 Python 3.10，当前为 {platform.python_version()}")
if struct.calcsize("P") != 8 or platform.machine().casefold() not in {"aarch64", "arm64"}:
    raise SystemExit(f"需要 Linux ARM64 Python，当前架构为 {platform.machine()}")
print("Python 环境:", sys.executable)
print("Python 版本:", platform.python_version(), platform.machine())
PY

requirements="$repo_root/enhanced_trainer/requirements-linux-aarch64.txt"
if [[ -n "$wheelhouse" ]]; then
  echo "=== 从离线 wheel 目录安装/校准组件依赖 ==="
  echo "$wheelhouse"
  "$python_path" -m pip install \
    --no-index \
    --find-links "$wheelhouse" \
    --requirement "$requirements"
fi

if ! "$python_path" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

expected = {
    "torch": "2.7.0+cpu",
    "onnx": "1.18.0",
    "onnxruntime": "1.19.2",
    "ddddocr": "1.5.6",
    "numpy": "2.0.2",
    "Pillow": "11.3.0",
    "cryptography": "45.0.7",
    "pyinstaller": "6.16.0",
}
problems = []
for name, wanted in expected.items():
    try:
        installed = version(name)
    except PackageNotFoundError:
        problems.append(f"{name} 未安装")
    else:
        if installed != wanted:
            problems.append(f"{name}={installed}，需要 {wanted}")
if problems:
    raise SystemExit("；".join(problems))

from enhanced_trainer.package_tool import _require_build_dependencies

_require_build_dependencies()
print("组件依赖检查通过")
PY
then
  cat >&2 <<'EOF'
错误：组件依赖不完整或版本不一致。
首次构建请使用 --wheelhouse 指定 Linux ARM64 离线 wheel 目录。
EOF
  exit 1
fi

"$python_path" -m pip check

echo "=== 运行组件源码自检 ==="
"$python_path" -m enhanced_trainer self-test --protocol-version 2

timestamp="$(date +%Y%m%d-%H%M%S)"
if [[ -z "$output_dir" ]]; then
  output_dir="$repo_root/output/components/$timestamp"
else
  output_dir="$(make_absolute "$output_dir")"
fi
mkdir -p "$output_dir"

if [[ -z "$build_root" ]]; then
  cache_home="${XDG_CACHE_HOME:-$HOME/.cache}"
  build_root="$cache_home/intdemo-trainer-build"
else
  build_root="$(make_absolute "$build_root")"
fi

package_name="IntDemo-Trainer-$component_version-linux-aarch64.inttrainer"
package_path="$output_dir/$package_name"
if [[ -e "$package_path" ]]; then
  echo "错误：输出组件已存在，已拒绝覆盖：$package_path" >&2
  exit 1
fi

echo "=== 构建 UOS ARM64 验证码组件（不构建客户端程序） ==="
"$python_path" "$repo_root/scripts/package-trainer.py" \
  --platform linux-aarch64 \
  all \
  --build-root "$build_root" \
  --output "$package_path"

"$python_path" -m zipfile -t "$package_path"
(
  cd "$output_dir"
  sha256sum "$package_name" > "$package_name.sha256"
)
package_hash="$(sha256sum "$package_path" | awk '{print $1}')"

echo "=== 组件打包完成 ==="
echo "组件：$package_path"
echo "校验：$package_path.sha256"
echo "SHA-256：$package_hash"
echo "本脚本只生成 .inttrainer 组件，没有打包客户端、DEB、安装程序或服务端。"
