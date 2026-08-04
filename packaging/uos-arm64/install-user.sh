#!/usr/bin/env bash
set -Eeuo pipefail

source_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install_root="$HOME/.local/opt/intdemo-client"
binary_dir="$HOME/.local/bin"
desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop_file="$desktop_dir/com.e23aqiu.intdemo.desktop"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/intdemo-client"
user_config="$config_dir/client-online.json"
user_cert_dir="$config_dir/certs"

case "$install_root" in
  "$HOME"/.local/opt/intdemo-client) ;;
  *) echo "拒绝写入非预期安装目录：$install_root" >&2; exit 1 ;;
esac

temporary_root="$HOME/.local/opt/.intdemo-client-new-$$"
rm -rf -- "$temporary_root"
mkdir -p "$temporary_root" "$binary_dir" "$desktop_dir" "$config_dir"
cp -a "$source_root/." "$temporary_root/"
rm -rf -- "$install_root"
mv "$temporary_root" "$install_root"
ln -sfn "$install_root/intdemo-client" "$binary_dir/intdemo-client"
sed "s|@INSTALL_DIR@|$install_root|g" \
  "$install_root/com.e23aqiu.intdemo.desktop" > "$desktop_file"
chmod +x \
  "$install_root/intdemo-client" \
  "$install_root/install-user.sh" \
  "$install_root/uninstall-user.sh" \
  "$install_root/app/intdemo-client"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

echo "安装完成：$install_root"
echo "可从应用菜单启动“逃费车辆智能查询平台”，或执行：intdemo-client"
echo "业务数据未写入安装目录，升级/卸载不会删除本地数据库。"
if [[ ! -f "$user_config" && -f "$install_root/client-online.json" ]]; then
  mkdir -p "$user_cert_dir"
  cp "$install_root/client-online.json" "$user_config"
  if [[ -f "$install_root/certs/intdemo-caddy-root.crt" ]]; then
    cp "$install_root/certs/intdemo-caddy-root.crt" \
      "$user_cert_dir/intdemo-caddy-root.crt"
  fi
  chmod 0600 "$user_config"
  chmod 0644 "$user_cert_dir/intdemo-caddy-root.crt" 2>/dev/null || true
  echo "在线服务已写入用户配置：$user_config"
elif [[ -f "$user_config" ]]; then
  echo "保留现有在线配置：$user_config"
fi
