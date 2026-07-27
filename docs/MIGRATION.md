# 迁往公司服务器

目标宿主必须能运行 Linux 容器。若公司只提供 Windows Server，先创建 Linux
虚拟机；不要把本 Compose 当作原生 Windows 容器运行。
相关平台限制参见
[Microsoft Windows 容器支持说明](https://learn.microsoft.com/en-us/troubleshoot/windows-server/containers/support-for-windows-containers-docker-on-premises-scenarios)。

## 固定迁移顺序

1. 发布维护通知。客户端停止新任务；离线客户端保留 SQLite outbox。
2. 等在线客户端待上传数量归零，在 `.env` 设置
   `INTDEMO_MAINTENANCE_MODE=1` 后执行 `docker compose up -d api`。
   服务端停止写入并返回可重试的 `maintenance_mode`，客户端继续保留 outbox。
3. 执行立即备份、验证 SHA-256，并加密复制：
   - PostgreSQL `.dump` 与 `.sha256`
   - `deploy/secrets/` 三个私密文件
   - `.env`、Caddy 配置及所用镜像 digest
4. 新服务器初始化 Docker 和防火墙，复制仓库与密钥。
5. 在新服务器恢复到全新数据库，执行 `alembic upgrade head`。
6. 核对每张核心表记录数及 `SELECT MAX(revision) FROM change_log`。
7. 域名方案先以临时 hosts/测试域名验证，再切 DNS；尽量保持原域名。
   IP 方案必须重新签发证书并更新客户端配置。
8. 保持防火墙仅允许验收来源，在新服务器设置
   `INTDEMO_MAINTENANCE_MODE=0` 并重建 API；两台客户端完成登录、推送、
   WebSocket 通知、拉取、设备上限和私有 CA 验收后再开放流量。
9. 旧服务器保持只读 7 天作为回退点，之后再安全下线。

## 回退

DNS 尚未切换时直接恢复旧 API。DNS 已切换时停止新服务器写入，确认没有
新变更后再切回；若新服务器已产生写入，必须先备份并决定数据合并策略，
不能直接用旧库覆盖。

## 迁移验收记录

至少记录：

- 旧/新服务器最新变更修订号
- accounts、devices、activity_events、workflow_batches、workflow_runs 数量
- 备份 SHA-256
- DNS 切换时间与 TTL
- 两台验收客户端设备 UID
- 旧服务器只读保留和下线时间
