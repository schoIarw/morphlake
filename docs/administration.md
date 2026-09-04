# 管理服务、Token、限流与监控

## 独立容器

同一镜像启动两个职责独立的进程：

| 容器 | 默认端口 | 职责 |
| --- | --- | --- |
| `morphlake-api` | 8080 | 业务 API、Token 验证、上传下载限流、MinIO/Paimon/模型、业务指标 |
| `morphlake-admin` | 8081 | 管理页面、Token 生命周期及配额配置、传输统计、Prometheus 查询 |

两个容器共享 Docker 命名卷 `morphlake-admin-data` 中的 SQLite 文件。SQLite 启用 WAL、
`busy_timeout` 和原子事务；API 与管理进程可独立重启。一个 Docker 主机内可增加 API 副本，
限流计数仍由同一个 SQLite 文件原子更新，审计批次使用租约避免副本重复消费。

SQLite 不适合跨主机共享文件。如果未来需要跨主机扩容，应保留 `AdminStore` 接口并将管理
存储替换为具备一致事务的网络数据库；不要通过 NFS 共享 SQLite，也不要为每个副本复制一份
配额库，否则无法保证全局限流准确。

## 首次启动

```bash
cp .env.example .env
# 必须修改管理密码、Token pepper、指标 Token 和 MinIO 配置
docker compose up --build -d
curl http://localhost:8080/health/live
curl http://localhost:8081/health/live
```

浏览器访问 `http://localhost:8081/admin`，使用 `MORPHLAKE_ADMIN_USERNAME` 和
`MORPHLAKE_ADMIN_PASSWORD` 进行 HTTP Basic 登录。

## Token 管理

管理台支持：

- 创建绑定到一个业务域和一个部门的 Token；
- 记录使用人姓名、手机号码、备注、分配人、创建时间、过期时间；
- 调整周期秒数、上传/下载周期次数、上传/下载周期字节数；
- 启用、停用和删除 Token；删除是不可恢复的软删除，以便 API 明确返回 `token_deleted`；
- 查看 Token 前缀，不保存或再次显示完整密钥。

Token 形如 `mlk_前缀_随机密钥`。SQLite 只保存带
`MORPHLAKE_TOKEN_PEPPER` 的 HMAC-SHA256 摘要，明文只在创建成功页显示一次。生产环境必须
备份 SQLite 数据卷和 pepper；丢失任意一项都无法验证已有 Token。

限流使用固定窗口，同时检查请求次数和字节数。0 表示不限制。所有判断在
`BEGIN IMMEDIATE` 事务内执行，超过任一配额返回 429 和 `Retry-After`。

## 传输明细与统计

每次上传、下载和限流拒绝都会记录：Token ID/前缀、业务域、部门、文件、媒体类型、字节数、
耗时、状态、错误码、客户端 IP 和 User-Agent。

API 先把事件与每日汇总写入 SQLite outbox，保证请求结束时已有可查记录；后台按批次写入
Paimon `multimodal_transfer_audit` 长期表，成功后标记同步。SQLite 默认保留已同步明细 30 天，
管理页提供天、周、月的条数和数据量统计。同步失败不会丢事件，Prometheus 的
`morphlake_transfer_audit_backlog` 会持续增长并触发运维关注。

outbox 采用“至少一次”写入：若进程在 Paimon 提交成功、SQLite 标记完成之前退出，恢复后可能
用同一个 `event_id` 重写该事件。审计查询或下游汇总应按 `event_id` 去重；相比“先标记再写”
造成永久丢失，这一取舍更适合审计数据。

## Prometheus

API 和管理服务的 `/metrics` 均要求独立的 `MORPHLAKE_METRICS_TOKEN`。业务 Token、管理密码
均不能代替指标 Token。示例配置：

```bash
cp monitoring/prometheus.yml.example /path/to/prometheus.yml
printf '%s' "$MORPHLAKE_METRICS_TOKEN" > /path/to/morphlake-metrics-token
chmod 600 /path/to/morphlake-metrics-token
```

将 token 文件挂载到 Prometheus 的 `/etc/prometheus/morphlake-metrics-token`，并确保
Prometheus 能通过容器网络解析 `morphlake-api` 和 `morphlake-admin`。

指标覆盖：

- HTTP 请求量与耗时直方图；
- 按业务域、部门、操作、状态聚合的上传下载请求数和字节数；
- Token 认证失败和限流拒绝；
- MinIO、Paimon、模型、索引与 SQLite 连通状态；
- Paimon 索引维护次数、耗时和最后成功时间；
- Paimon 审计 outbox 积压与同步结果；
- Python 进程 CPU、内存、GC 和运行时指标。

指标标签不包含完整 Token、姓名、手机、文件名或文件 ID，避免隐私泄露和高基数。

## Grafana 与内置监控页

在 Grafana 导入 `monitoring/grafana/morphlake-dashboard.json`，导入时选择 Prometheus 数据源。
面板包含实例状态、API 速率、上传下载流量、P95 时延、依赖连通性、索引状态、审计积压及
进程资源。

设置 `PROMETHEUS_URL` 后，管理页 `http://localhost:8081/admin/monitoring` 会通过 Prometheus
HTTP API 查询关键指标，在 MorphLake 内直接查看当前状态。该页面不会绕过 Prometheus 的
保留策略，也不替代 Grafana 告警。
