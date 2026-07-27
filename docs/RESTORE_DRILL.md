# PostgreSQL 备份与恢复演练

`backup` 服务每天北京时间 02:00 执行 `pg_dump -Fc`，用 `pg_restore --list`
验证格式，生成 SHA-256 校验文件并保留 30 天。测试期至少每周把一份
`.dump` 与对应 `.sha256` 复制到服务器外；正式环境复制到公司 NAS/备份盘。

## 立即备份

```bash
docker compose run --rm backup /bin/bash /scripts/backup-now.sh
docker compose run --rm backup find /backups -maxdepth 1 -type f -ls
```

如需复制到宿主机：

```bash
mkdir -p /srv/intdemo-offsite-staging
docker compose run --rm \
  -v /srv/intdemo-offsite-staging:/export \
  backup /bin/bash -c \
  'cp "$(find /backups -name "intdemo-*.dump" | sort | tail -1)"* /export/'
```

## 隔离恢复演练

以下命令只替换专用的 `intdemo_restore` 数据库，不接触生产 `intdemo`：

```bash
docker compose run --rm backup /bin/bash /scripts/restore-latest.sh
```

脚本会先验证 SHA-256，再创建干净数据库、执行 `pg_restore
--exit-on-error`，最后逐项比对源库与恢复库的账号、设备、活动事件、批次、
运行、变更记录数以及最新全局修订号。CI 也在全新 Compose 环境执行同一演练。

## 生产恢复

1. 进入维护窗口并停止 API 写入：`docker compose stop api`。
2. 对目标 `.dump` 手工执行 `sha256sum --check`。
3. 先恢复到 `intdemo_restore` 并核对表记录数与最新 `change_log.revision`。
4. 只有核对成功后才将正式数据库名作为恢复目标：

```bash
docker compose run --rm \
  -e CONFIRM_RESTORE=YES \
  backup /bin/bash /scripts/restore.sh \
  /backups/intdemo-YYYYMMDD-HHMMSS.dump intdemo
docker compose run --rm api alembic upgrade head
docker compose start api
```

`restore.sh` 会删除并重建明确给出的目标数据库，这是有意的破坏性操作；
禁止使用 `postgres`、`template0` 或 `template1` 作为目标。

自定义格式及跨架构恢复行为参见
[PostgreSQL pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html)。
