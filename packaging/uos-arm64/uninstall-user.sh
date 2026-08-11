#!/usr/bin/env bash
set -Eeuo pipefail

install_root="$HOME/.local/opt/intdemo-client"
binary_link="$HOME/.local/bin/intdemo-client"
desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop_file="$desktop_dir/com.e23aqiu.intdemo.desktop"

case "$install_root" in
  "$HOME"/.local/opt/intdemo-client) ;;
  *) echo "拒绝清理非预期安装目录：$install_root" >&2; exit 1 ;;
esac
rm -f -- "$binary_link" "$desktop_file"
rm -rf -- "$install_root"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

echo "程序已卸载。"
echo "本地业务数据仍保留在：${XDG_DATA_HOME:-$HOME/.local/share}/intdemo-client-online-test"
