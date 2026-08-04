逃费车辆智能查询平台 - UOS ARM64 DEB
========================================

程序安装位置：
  /opt/apps/com.e23aqiu.intdemo/files/

启动方式：
  从应用菜单启动“逃费车辆智能查询平台（UOS）”。
  DEB 桌面入口与包 AppID 同名；安装前请先卸载历史用户级安装。

在线服务：
  默认在线测试服务及其公开根证书已随包配置。
  ~/.config/intdemo-client/client-online.json 若已存在，则优先使用用户配置。

用户数据：
  ~/.local/share/intdemo-client-online-test/

升级：
  直接安装更高版本的 DEB，不会覆盖用户数据或用户在线配置。

卸载：
  sudo apt remove com.e23aqiu.intdemo

卸载程序不会删除用户数据库、浏览器资料或在线配置。
