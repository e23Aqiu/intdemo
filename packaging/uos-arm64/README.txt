逃费车辆智能查询平台 - 统信 UOS 20 ARM64
============================================

快速试运行：
  1. 确认系统已安装 secret-tool；Chromium 和 Qt 所需 GNU C++ 运行库已包含在本软件包中。
  2. 按 UOS_ARM64.md 创建 ~/.config/intdemo-client/client-online.json。
  3. 在当前目录执行：./intdemo-client
  4. 仅在排查内置浏览器问题时才覆盖浏览器路径：
       export INTDEMO_CHROMIUM_PATH=/实际/浏览器/可执行文件
       ./intdemo-client

安装到当前用户：
  ./install-user.sh

卸载程序（不会删除业务数据库）：
  ~/.local/opt/intdemo-client/uninstall-user.sh

日志目录：
  ~/.local/share/intdemo-client-online-test/logs

完整的构建、诊断与验收说明见 UOS_ARM64.md。
