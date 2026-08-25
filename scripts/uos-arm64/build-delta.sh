#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
source_deb=""
target_deb=""
from_version=""
target_version=""
source_receipt=""
output=""
report=""
threshold_percent=50

usage() {
  cat <<'EOF'
用法：bash scripts/uos-arm64/build-delta.sh [选项]
  --source-deb PATH       真实已发布的来源版本完整 DEB
  --target-deb PATH       当前目标版本完整 DEB
  --from-version X.Y.Z    来源版本
  --target-version X.Y.Z  目标版本
  --source-receipt PATH   来源版本 publish-receipt.json（正式构建必填）
  --output PATH           输出 .intdelta；默认写入 dist/uos-arm64/
  --report PATH           基准报告 JSON；默认与补丁同名并追加 .json
  --threshold-percent N   最大补丁占完整包百分比，默认 50

设置 INTDEMO_XDELTA3_PATH 可指定构建机上的 xdelta3。最终用户无需安装它；
UOS 客户端完整 DEB 会另行内置 ARM64 xdelta3。
EOF
}

absolute_path() {
  local value="$1"
  if [[ "$value" == /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$repo_root/$value"
  fi
}

while (($#)); do
  case "$1" in
    --source-deb) source_deb="${2:-}"; shift ;;
    --target-deb) target_deb="${2:-}"; shift ;;
    --from-version) from_version="${2:-}"; shift ;;
    --target-version) target_version="${2:-}"; shift ;;
    --source-receipt) source_receipt="${2:-}"; shift ;;
    --output) output="${2:-}"; shift ;;
    --report) report="${2:-}"; shift ;;
    --threshold-percent) threshold_percent="${2:-}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ ! "$from_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误：--from-version 必须使用 x.y.z 格式。" >&2
  exit 2
fi
if [[ ! "$target_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误：--target-version 必须使用 x.y.z 格式。" >&2
  exit 2
fi
if [[ ! "$threshold_percent" =~ ^[0-9]+$ ]] || \
   ((threshold_percent < 1 || threshold_percent > 100)); then
  echo "错误：--threshold-percent 必须是 1～100 的整数。" >&2
  exit 2
fi

source_deb="$(absolute_path "$source_deb")"
target_deb="$(absolute_path "$target_deb")"
[[ -f "$source_deb" ]] || { echo "错误：来源 DEB 不存在：$source_deb" >&2; exit 2; }
[[ -f "$target_deb" ]] || { echo "错误：目标 DEB 不存在：$target_deb" >&2; exit 2; }
if [[ -n "$source_receipt" ]]; then
  source_receipt="$(absolute_path "$source_receipt")"
  [[ -f "$source_receipt" ]] || {
    echo "错误：来源发布收据不存在：$source_receipt" >&2
    exit 2
  }
elif [[ "${INTDEMO_ALLOW_UNRECEIPTED_DELTA:-0}" != "1" ]]; then
  echo "错误：正式补丁必须提供 --source-receipt。" >&2
  echo "仅基准测试可设置 INTDEMO_ALLOW_UNRECEIPTED_DELTA=1。" >&2
  exit 2
fi

for command_name in dpkg dpkg-deb sha256sum stat cmp python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "错误：构建机缺少 $command_name。" >&2
    exit 1
  }
done
xdelta3_path="${INTDEMO_XDELTA3_PATH:-}"
if [[ -z "$xdelta3_path" ]]; then
  xdelta3_path="$(command -v xdelta3 || true)"
fi
if [[ -z "$xdelta3_path" || ! -x "$xdelta3_path" ]]; then
  echo "错误：构建机缺少 xdelta3，请安装构建依赖或设置 INTDEMO_XDELTA3_PATH。" >&2
  exit 1
fi

source_package="$(dpkg-deb -f "$source_deb" Package)"
target_package="$(dpkg-deb -f "$target_deb" Package)"
source_arch="$(dpkg-deb -f "$source_deb" Architecture)"
target_arch="$(dpkg-deb -f "$target_deb" Architecture)"
source_package_version="$(dpkg-deb -f "$source_deb" Version)"
target_package_version="$(dpkg-deb -f "$target_deb" Version)"
if [[ "$source_package" != "com.e23aqiu.intdemo" || \
      "$target_package" != "com.e23aqiu.intdemo" ]]; then
  echo "错误：来源和目标必须是 com.e23aqiu.intdemo DEB。" >&2
  exit 1
fi
if [[ "$source_arch" != "arm64" || "$target_arch" != "arm64" ]]; then
  echo "错误：来源和目标 DEB 架构必须为 arm64。" >&2
  exit 1
fi
if [[ "$source_package_version" != "$from_version" || \
      "$target_package_version" != "$target_version" ]]; then
  echo "错误：DEB Version 与命令行来源/目标版本不一致。" >&2
  exit 1
fi
if dpkg --compare-versions "$from_version" ge "$target_version"; then
  echo "错误：来源版本必须低于目标版本。" >&2
  exit 1
fi

source_size="$(stat -c '%s' "$source_deb")"
target_size="$(stat -c '%s' "$target_deb")"
source_sha256="$(sha256sum "$source_deb" | awk '{print $1}')"
target_sha256="$(sha256sum "$target_deb" | awk '{print $1}')"

if [[ -n "$source_receipt" ]]; then
  mapfile -t receipt_values < <(
    python3 - "$source_receipt" "$from_version" <<'PY'
import json
import sys

path, expected_version = sys.argv[1:]
with open(path, encoding="utf-8") as source:
    payload = json.load(source)
if payload.get("version") != expected_version:
    raise SystemExit("来源发布收据版本不匹配")
artifact = payload.get("artifacts", {}).get("uos_installer")
if not isinstance(artifact, dict):
    raise SystemExit("来源发布收据缺少 UOS 安装包")
print(str(artifact.get("name") or ""))
print(str(artifact.get("size") or ""))
print(str(artifact.get("sha256") or "").lower())
PY
  )
  if ((${#receipt_values[@]} != 3)); then
    echo "错误：无法读取来源发布收据。" >&2
    exit 1
  fi
  if [[ "${receipt_values[0]}" != "$(basename "$source_deb")" || \
        "${receipt_values[1]}" != "$source_size" || \
        "${receipt_values[2]}" != "$source_sha256" ]]; then
    echo "错误：来源 DEB 与正式发布收据不一致，禁止重建旧版本代替真实产物。" >&2
    exit 1
  fi
fi

if [[ -z "$output" ]]; then
  output="$repo_root/dist/uos-arm64/IntDemo-UOS-arm64-Patch-$from_version-to-$target_version.intdelta"
else
  output="$(absolute_path "$output")"
fi
if [[ "${output,,}" != *.intdelta ]]; then
  echo "错误：--output 必须使用 .intdelta 后缀。" >&2
  exit 2
fi
if [[ -z "$report" ]]; then
  report="$output.json"
else
  report="$(absolute_path "$report")"
fi
mkdir -p "$(dirname "$output")" "$(dirname "$report")"
temporary_patch="$output.part"
rebuilt="$output.rebuilt.deb"
trap 'rm -f -- "$temporary_patch" "$rebuilt"' EXIT
# A rejected or interrupted rebuild must never leave a previously generated
# patch looking like the result of the current command.
rm -f -- "$output" "$temporary_patch" "$rebuilt"

echo "=== 生成 UOS ARM64 增量包 ==="
encode_started="$(date +%s)"
"$xdelta3_path" -f -S djw -e -s "$source_deb" "$target_deb" "$temporary_patch"
encode_seconds="$(( $(date +%s) - encode_started ))"
patch_size="$(stat -c '%s' "$temporary_patch")"

echo "=== 回放并逐字节验证目标 DEB ==="
decode_started="$(date +%s)"
"$xdelta3_path" -f -d -s "$source_deb" "$temporary_patch" "$rebuilt"
decode_seconds="$(( $(date +%s) - decode_started ))"
rebuilt_size="$(stat -c '%s' "$rebuilt")"
rebuilt_sha256="$(sha256sum "$rebuilt" | awk '{print $1}')"
if [[ "$rebuilt_size" != "$target_size" || "$rebuilt_sha256" != "$target_sha256" ]] || \
   ! cmp -s "$target_deb" "$rebuilt"; then
  echo "错误：增量包无法逐字节重建正式目标 DEB。" >&2
  exit 1
fi

ratio_basis_points="$((patch_size * 10000 / target_size))"
eligible=true
if ((patch_size * 100 >= target_size * threshold_percent)); then
  eligible=false
fi
patch_sha256="$(sha256sum "$temporary_patch" | awk '{print $1}')"

python3 - \
  "$report" \
  "$from_version" \
  "$target_version" \
  "$(basename "$source_deb")" \
  "$source_size" \
  "$source_sha256" \
  "$(basename "$target_deb")" \
  "$target_size" \
  "$target_sha256" \
  "$(basename "$output")" \
  "$patch_size" \
  "$patch_sha256" \
  "$ratio_basis_points" \
  "$threshold_percent" \
  "$eligible" \
  "$encode_seconds" \
  "$decode_seconds" <<'PY'
import json
import os
import sys

(
    path,
    from_version,
    target_version,
    base_name,
    base_size,
    base_sha256,
    target_name,
    target_size,
    target_sha256,
    patch_name,
    patch_size,
    patch_sha256,
    ratio_basis_points,
    threshold_percent,
    eligible,
    encode_seconds,
    decode_seconds,
) = sys.argv[1:]
payload = {
    "schema_version": 1,
    "format": "uos-deb-xdelta-v1",
    "algorithm": "xdelta3",
    "from_version": from_version,
    "target_version": target_version,
    "base_name": base_name,
    "base_size": int(base_size),
    "base_sha256": base_sha256,
    "target_name": target_name,
    "target_size": int(target_size),
    "target_sha256": target_sha256,
    "patch_name": patch_name,
    "patch_size": int(patch_size),
    "patch_sha256": patch_sha256,
    "ratio_percent": int(ratio_basis_points) / 100,
    "threshold_percent": int(threshold_percent),
    "eligible": eligible == "true",
    "encode_seconds": int(encode_seconds),
    "decode_seconds": int(decode_seconds),
    "byte_identical": True,
}
temporary = path + ".part"
with open(temporary, "w", encoding="utf-8") as target:
    json.dump(payload, target, ensure_ascii=False, indent=2)
    target.write("\n")
os.replace(temporary, path)
PY

if [[ "$eligible" != true ]]; then
  echo "补丁占完整包 $((ratio_basis_points / 100)).$((ratio_basis_points % 100))%，达到或超过 ${threshold_percent}% 门槛。"
  echo "本来源版本不发布增量包，保留基准报告：$report"
  exit 0
fi

mv -f -- "$temporary_patch" "$output"
trap 'rm -f -- "$rebuilt"' EXIT
echo "补丁：$output"
echo "报告：$report"
echo "完整包：$target_size 字节；补丁：$patch_size 字节；占比：$((ratio_basis_points / 100)).$((ratio_basis_points % 100))%"
echo "重建校验：逐字节一致；SHA-256=$target_sha256"
