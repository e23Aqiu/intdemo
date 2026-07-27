# 同步协议与隐私边界

## 同步项

`POST /api/v1/sync/push` 每批最多 100 项，单项最大 64 KiB：

```json
{
  "schema_version": 1,
  "event_uid": "UUID",
  "kind": "activity_event",
  "entity_id": "client-stable-id",
  "revision": 1,
  "occurred_at": "2026-07-27T10:00:00+08:00",
  "payload": {}
}
```

`kind` 只允许：

- `activity_event`
- `workflow_batch_snapshot`
- `workflow_run_snapshot`

活动事件按账号和 `event_uid` 幂等；快照只接受更高修订，`succeeded` 不能回退
到运行态。永久非法项返回 `rejected/retryable=false`，客户端移入隔离区。

## 允许载荷

- 活动：指标键、整数数量、业务日期、安全来源标识、安全任务 ID、行数和违规
  原因数量汇总。
- 批次：状态、SHA-256 输入指纹、日期、开始/完成时间、有效/暂停/总用时、
  重试次数及指标计数。
- 运行：批次 ID、状态、步骤序号、开始/结束时间、有效/暂停/总用时和重试数。

服务端以严格白名单拒绝额外字段；嵌套汇总只允许整数。客户端在同一 SQLite
事务中先剥离本地明细再写 outbox。

## 永不上传

原始 Excel、文件名和路径、逐行车辆数据、车牌、运输证号、公司、法人、
地址、电话、Cookie、浏览器登录状态、运行日志和步骤错误明细。

## 拉取

WebSocket 仅发送全局修订变化通知，不携带业务数据。客户端随后调用
`GET /sync/pull?after_revision=N`；30 秒轮询为兜底。`stats_scope=own` 只能
得到本人数据，`all` 和管理员可以得到全部站点汇总。

账号归档和统计重置写入 `change_log`。重置产生带时间的墓碑，服务器永久拒绝
更早的离线项，客户端同时清除对应缓存和隔离旧 outbox。

## 固定错误格式

```json
{
  "code": "machine_readable_code",
  "message": "用户可读信息",
  "retryable": false,
  "details": null,
  "request_id": "UUID"
}
```

