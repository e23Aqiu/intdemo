#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH= cd -- "$script_dir/../.." && pwd)"
app_id="com.e23aqiu.intdemo"
desktop_file_name="$app_id.desktop"
version=""
package_root=""

usage() {
  cat <<'EOF'
用法：bash scripts/uos-arm64/build-deb.sh [选项]
  --package-root PATH  指定已生成的 UOS 便携包目录
  --version VERSION    指定三段式客户端版本

默认读取 integrated_client/config.py 中的版本，并使用：
  dist/uos-arm64/package/IntDemo-UOS-arm64-<版本>
EOF
}

while (($#)); do
  case "$1" in
    --package-root)
      (($# >= 2)) || { echo "错误：--package-root 缺少路径。" >&2; exit 2; }
      package_root="$2"
      shift
      ;;
    --version)
      (($# >= 2)) || { echo "错误：--version 缺少版本号。" >&2; exit 2; }
      version="$2"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ -z "$version" ]]; then
  version="$(
    tr -d '\r' < "$repo_root/integrated_client/config.py" | \
      sed -n 's/^APP_VERSION = "\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)"$/\1/p' | \
      head -n 1
  )"
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "错误：无法识别客户端版本：$version" >&2
  exit 1
fi

package_parent="$repo_root/dist/uos-arm64/package"
if [[ -z "$package_root" ]]; then
  package_root="$package_parent/IntDemo-UOS-arm64-$version"
elif [[ "$package_root" != /* ]]; then
  package_root="$repo_root/$package_root"
fi
if [[ ! -d "$package_root" ]]; then
  echo "错误：未找到 ARM64 便携包目录：$package_root" >&2
  echo "请先运行 scripts/uos-arm64/build.sh 生成 ARM64 成品。" >&2
  exit 1
fi
package_root="$(readlink -f "$package_root")"
package_parent="$(readlink -f "$package_parent")"
case "$package_root" in
  "$package_parent"/IntDemo-UOS-arm64-*) ;;
  *) echo "拒绝读取非预期便携包目录：$package_root" >&2; exit 1 ;;
esac

required_paths=(
  "$package_root/app/intdemo-client"
  "$package_root/app/_internal/libstdc++.so.6"
  "$package_root/browser/chrome"
  "$package_root/build-info.txt"
  "$package_root/layer-layout.json"
  "$package_root/client-online.json"
)
for required_path in "${required_paths[@]}"; do
  if [[ ! -e "$required_path" ]]; then
    echo "错误：便携包缺少 $required_path" >&2
    echo "请先运行 scripts/uos-arm64/build.sh 生成 ARM64 成品。" >&2
    exit 1
  fi
done
if ! grep -Fxq "version=$version" "$package_root/build-info.txt"; then
  echo "错误：便携包版本与目标版本 $version 不一致。" >&2
  exit 1
fi
if ! grep -Eq '^architecture=(aarch64|arm64)$' "$package_root/build-info.txt"; then
  echo "错误：build-info.txt 未确认 ARM64 架构。" >&2
  exit 1
fi

for command_name in dpkg-deb md5sum readelf sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "错误：缺少构建命令 $command_name。" >&2
    exit 1
  fi
done
if ! LC_ALL=C readelf -h "$package_root/app/intdemo-client" | \
  grep -E '^[[:space:]]*Machine:[[:space:]]*AArch64[[:space:]]*$' \
    >/dev/null; then
  echo "错误：便携包入口不是 AArch64 ELF。" >&2
  exit 1
fi

artifact="$repo_root/dist/uos-arm64/IntDemo-UOS-arm64-$version.deb"
staging_parent="$repo_root/dist/uos-arm64/deb-root"
deb_root="$staging_parent/$app_id-$version-arm64"
app_root="$deb_root/opt/apps/$app_id"
files_root="$app_root/files"
desktop_root="$app_root/entries/applications"

case "$deb_root" in
  "$repo_root"/dist/uos-arm64/deb-root/$app_id-*-arm64) ;;
  *) echo "拒绝清理非预期 DEB 暂存目录：$deb_root" >&2; exit 1 ;;
esac
case "$artifact" in
  "$repo_root"/dist/uos-arm64/IntDemo-UOS-arm64-*.deb) ;;
  *) echo "拒绝清理非预期 DEB 产物：$artifact" >&2; exit 1 ;;
esac

deb_complete=0
cleanup_deb_build() {
  rm -rf -- "$deb_root"
  rmdir "$staging_parent" 2>/dev/null || true
  if ((deb_complete == 0)); then
    rm -f -- "$artifact" "$artifact.sha256"
  fi
}
trap cleanup_deb_build EXIT

echo "=== 组装 UOS ARM64 DEB ==="
rm -rf -- "$deb_root"
rm -f -- "$artifact" "$artifact.sha256"
mkdir -p "$deb_root/DEBIAN" "$files_root" "$desktop_root"

cp -a "$package_root/app" "$files_root/"
cp -a "$package_root/browser" "$files_root/"
mkdir -p "$files_root/certs"
cp "$package_root/client-online.json" "$files_root/"
if [[ -d "$package_root/certs" ]]; then
  cp -a "$package_root/certs/." "$files_root/certs/"
fi
cp "$repo_root/packaging/uos-arm64/intdemo-client" "$files_root/"
cp "$repo_root/packaging/uos-arm64/deb/README.txt" "$files_root/"
cp "$repo_root/docs/UOS_ARM64.md" "$files_root/"
cp "$repo_root/integrated_client/ui/assets/app-icon.png" "$files_root/"
cp "$package_root/build-info.txt" "$files_root/"
cp "$package_root/layer-layout.json" "$files_root/"
cp "$repo_root/packaging/uos-arm64/deb/$desktop_file_name" \
  "$desktop_root/"

sed "s/@UOS_VERSION@/$version.0/g" \
  "$repo_root/packaging/uos-arm64/deb/info.json.in" > "$app_root/info"

chmod 0755 \
  "$files_root/intdemo-client" \
  "$files_root/app/intdemo-client" \
  "$files_root/browser/chrome"
chmod 0644 \
  "$app_root/info" \
  "$desktop_root/$desktop_file_name" \
  "$files_root/client-online.json" \
  "$files_root/README.txt" \
  "$files_root/UOS_ARM64.md" \
  "$files_root/app-icon.png" \
  "$files_root/build-info.txt" \
  "$files_root/layer-layout.json"
find "$files_root/certs" -type f -exec chmod 0644 {} +
find "$deb_root" -type d -exec chmod 0755 {} +

installed_size="$(du -sk "$app_root" | awk '{print $1}')"
sed \
  -e "s/@VERSION@/$version/g" \
  -e "s/@INSTALLED_SIZE@/$installed_size/g" \
  "$repo_root/packaging/uos-arm64/deb/control.in" > "$deb_root/DEBIAN/control"

(
  cd "$deb_root"
  find opt -type f -print0 | LC_ALL=C sort -z | xargs -0 md5sum
) > "$deb_root/DEBIAN/md5sums"
chmod 0644 "$deb_root/DEBIAN/control" "$deb_root/DEBIAN/md5sums"

if dpkg-deb --help 2>&1 | grep -F -- '--root-owner-group' >/dev/null; then
  dpkg-deb --root-owner-group --build "$deb_root" "$artifact"
elif command -v fakeroot >/dev/null 2>&1; then
  fakeroot dpkg-deb --build "$deb_root" "$artifact"
else
  echo "错误：当前 dpkg-deb 不支持 --root-owner-group，且未安装 fakeroot。" >&2
  echo "请执行 sudo apt install -y fakeroot 后重试。" >&2
  exit 1
fi

if [[ "$(dpkg-deb --field "$artifact" Package)" != "$app_id" ]]; then
  echo "错误：DEB 包名校验失败。" >&2
  exit 1
fi
if [[ "$(dpkg-deb --field "$artifact" Version)" != "$version" ]]; then
  echo "错误：DEB 版本校验失败。" >&2
  exit 1
fi
if [[ "$(dpkg-deb --field "$artifact" Architecture)" != "arm64" ]]; then
  echo "错误：DEB 架构校验失败。" >&2
  exit 1
fi
if ! dpkg-deb --contents "$artifact" | \
  grep -F "./opt/apps/$app_id/files/intdemo-client" >/dev/null; then
  echo "错误：DEB 缺少应用入口。" >&2
  exit 1
fi
if ! dpkg-deb --contents "$artifact" | \
  grep -F "./opt/apps/$app_id/entries/applications/$desktop_file_name" \
    >/dev/null; then
  echo "错误：DEB 缺少与 AppID 同名的 UOS 桌面入口。" >&2
  exit 1
fi

sha256sum "$artifact" > "$artifact.sha256"
deb_complete=1
cleanup_deb_build
trap - EXIT

echo "=== UOS ARM64 DEB 构建完成 ==="
echo "$artifact"
cat "$artifact.sha256"
du -sh "$artifact"
