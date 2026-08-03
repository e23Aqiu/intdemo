逃费车辆智能查询平台 - 统信 UOS 20 ARM64
============================================

快速试运行：
  1. 确认已安装 Chromium/UOS 浏览器和 secret-tool。
  2. 按 UOS_ARM64.md 创建 ~/.config/intdemo-client/client-online.json。
  3. 在当前目录执行：./intdemo-client
  4. 如果没有自动找到浏览器：
       export INTDEMO_CHROMIUM_PATH=/实际/浏览器/可执行文件
       ./intdemo-client

安装到当前用户：
  ./install-user.sh

卸载程序（不会删除业务数据库）：
  ~/.local/opt/intdemo-client/uninstall-user.sh

日志目录：
  ~/.local/share/intdemo-client-online-test/logs

完整的构建、诊断与验收说明见 UOS_ARM64.md。
