# 管理服务、API Key、限流与监控

## 独立容器

同一镜像启动两个职责独立的进程：

| 容器 | 默认端口 | 职责 |
| --- | --- | --- |
| `morphlake-api` | 8080 | 业务 API、Key 验证、上传下载限流、MinIO/Paimon/模型、业务指标 |
| `morphlake-admin` | 8081 | 登录式管理页面、API 操作台、Key 生命周期及配额配置、传输统计、Prometheus 查询 |

管理数据支持 SQLite、MySQL 和 PostgreSQL，三种后端共用同一个 `AdminStore` 接口和表模型。
SQLite 启用 WAL、`busy_timeout` 和原子事务，适合单个 Docker 主机；MySQL/PostgreSQL 使用连接池、
行锁和 `SKIP LOCKED`，适合 API 跨主机扩容。审计批次均使用数据库租约避免副本重复消费。

SQLite 模式下两个容器共享 Docker 命名卷 `morphlake-admin-data`。不要通过 NFS 共享 SQLite，
也不要为每个 API 副本复制配额库，否则无法保证全局限流准确。跨主机部署时应切换到 MySQL
或 PostgreSQL，API 和管理容器连接同一个数据库。

## 数据库配置

活动配置路径由 `MORPHLAKE_ADMIN_DB_CONFIG` 指定，容器默认使用
`/app/config/database.yaml`。仓库提供：

| 文件 | 用途 |
| --- | --- |
| `config/database.yaml` | 默认 SQLite |
| `config/database.mysql.native.yaml` | MySQL 明文凭据 |
| `config/database.mysql.encrypt.yaml` | MySQL 加密凭据 |
| `config/database.postgresql.native.yaml` | PostgreSQL 明文凭据 |
| `config/database.postgresql.encrypt.yaml` | PostgreSQL 加密凭据 |

`native` 模式直接填写用户名和密码：

```yaml
database:
  backend: mysql
  host: mysql.example.internal
  port: 3306
  name: morphlake
  auth:
    mode: native
    username: morphlake
    password: change-me
  auto_create_database: true
```

`encrypt` 模式在 YAML 保存经过认证的 Fernet 密文，密钥通过环境变量提供：

```yaml
database:
  backend: postgresql
  host: postgresql.example.internal
  port: 5432
  name: morphlake
  auth:
    mode: encrypt
    username: ENC[gAAAA...]
    password: ENC[gAAAA...]
    key_env: MORPHLAKE_DB_CREDENTIAL_KEY
  auto_create_database: true
  maintenance_database: postgres
  ssl_mode: require
```

生成密钥并手工生成可复制的密文：

```bash
export MORPHLAKE_DB_CREDENTIAL_KEY="$(python scripts/encrypt_db_credentials.py generate-key)"
python scripts/encrypt_db_credentials.py encrypt
```

脚本通过隐藏输入读取用户名和密码。密钥不要写入 `database.yaml` 或提交到 Git；正式环境使用
容器 Secret、Kubernetes Secret 或权限为 `600` 的环境文件。密钥丢失后无法解密已有配置。

首次启动默认执行以下幂等操作：

1. SQLite 创建数据库文件；MySQL/PostgreSQL 在 `auto_create_database: true` 时创建目标库；
2. 使用数据库级启动锁防止 API 与管理容器同时初始化产生竞争；
3. 创建 `api_tokens`、`rate_counters`、`transfer_events`、`transfer_daily_stats`、
   `system_config`、`admin_sessions` 六张表和索引；
4. 写入 schema 版本、首次初始化时间、数据库类型和默认上传下载配额；
5. 仅在第一次初始化时创建业务域/部门均为“管理员”的默认管理 Key。

MySQL 自动建库账号需要 `CREATE` 权限；PostgreSQL 账号需要 `CREATEDB` 且能连接
`maintenance_database`。数据库登录账号本身需由 DBA 预先建立；如果库也由 DBA 预建，设置
`auto_create_database: false`，应用仍会自动建表、建索引和初始化必要配置数据。

## 首次启动

```bash
cp .env.example .env
# 必须修改管理密码、会话密钥、Key pepper、Key 加密密钥、指标 Key 和 MinIO 配置
docker compose up --build -d
curl http://localhost:8080/health/live
curl http://localhost:8081/health/live
```

浏览器访问 `http://localhost:8081` 即会自动进入登录页，无需手工输入 `/admin`。使用
`MORPHLAKE_ADMIN_USERNAME` 和 `MORPHLAKE_ADMIN_PASSWORD` 登录后，页面采用窄头部、左侧菜单、
右侧工作区布局，并兼容桌面与移动浏览器。管理台参考 new-api 经典界面的信息密度与视觉习惯：
浅色导航、圆角卡片、紧凑字号和统一高度的输入控件；桌面端筛选项优先横向排列，窄屏时自动折行。
该改造未引入新的前端框架或运行组件。

登录成功后，服务端在管理数据库创建随机会话；浏览器 Cookie 仅保存随机值，带 `HttpOnly`、
`SameSite=Lax` 属性，数据库仅保存带 `MORPHLAKE_ADMIN_SESSION_SECRET` 的 HMAC-SHA256 摘要。
所有修改操作使用会话级 CSRF token。生产 HTTPS 环境设置：

```bash
MORPHLAKE_ADMIN_SESSION_SECRET=replace-with-a-separate-long-random-secret
MORPHLAKE_ADMIN_SESSION_TTL_SECONDS=28800
MORPHLAKE_ADMIN_COOKIE_SECURE=true
```

## API 操作台

管理容器通过 `MORPHLAKE_API_BASE_URL`（Compose 默认 `http://morphlake-api:8080`）调用现有 API，
不直接调用 MinIO、Paimon 或模型层，不重复业务实现。左侧菜单提供：

| 页面 | 转发到已有 API |
| --- | --- |
| 文件上传 | `POST /api/v1/files` 及 documents/images/audio 类型端点 |
| 文件清单 | `GET /api/v1/admin/files`；自动显示最近清单、总数和翻页 |
| 文件删除 | `POST /api/v1/files/batch-delete`；清单复选框组合调用 |
| 全文检索 | `POST /api/v1/search/full-text` |
| 向量检索 | `POST /api/v1/search/vector`、`POST /api/v1/search/vector/file` |
| 文件下载 | `GET /api/v1/files/{file_id}/download`，流式转发 |
| API 状态 | `GET /health/ready` |

上传、全文/向量检索等页面要求业务 API Key。为避免反复输入，前端仅在当前浏览器标签页的
`sessionStorage` 保存 Key，退出管理系统时清除；API 操作请求不会把 Key 写入日志或结果页面。
文件清单是管理员专属页面，管理容器在服务端从管理数据库解密默认管理 Key，直接加载最近数据，
并支持业务域、部门、文件名、文本概要、类型和日期筛选以及分页；管理 Key 不下发到页面。业务域
与部门筛选项从管理数据库中的 Key 和历史传输记录去重生成下拉选项，并包含“管理员”范围，选择业务域后部门下拉只显示
该域已有部门。Key 分配页使用相同数据生成可输入的下拉建议，以便复用已有范围，同时保留首次创建
或新增业务范围的能力。
配置项：

上传、清单、全文、向量和状态查询提交后，按钮会被禁用并显示转圈状态，阻止重复提交。查询结果
按照模态组合调用既有 API：图片显示缩略图并可放大，文档可查看提取文本，音频可在线播放，
三类文件均可下载；查看动作位于预览框，下载动作位于文件名。清单以按逗号换行的文本显示向量
前 8 位和完整维度，创建时间按浏览器时区转换并将日期、时分秒分两行展示。每页条数位于表格
底部；管理员可使用复选框和顶部按钮批量删除文件。

```bash
MORPHLAKE_API_BASE_URL=http://morphlake-api:8080
MORPHLAKE_ADMIN_API_TIMEOUT_SECONDS=30
```

## Key 管理与默认管理 Key

管理台支持：

- 首次初始化自动创建一个 `access_level=admin` 的管理 Key，其业务域和部门均为“管理员”；
- 管理 Key 查询时不附加业务范围过滤，可查询和下载全部业务域数据；
- 创建的普通 Key 绑定一个业务域和部门：上传继承两项归属，查询自动限制到所属业务域；
- 记录使用人姓名、手机号码、备注、分配人、创建时间、过期时间；
- 调整周期秒数、上传/下载周期次数、上传/下载周期字节数；
- 查看、隐藏和复制完整 Key，并可重新生成；重新生成后旧 Key 立即失效；
- 启用、停用和删除业务域 Key；默认管理 Key 不允许删除。

Key 形如 `mlk_前缀_随机密钥`。管理数据库同时保存：用于快速认证的
`MORPHLAKE_TOKEN_PEPPER` HMAC-SHA256 摘要，以及用于管理界面查看的 Fernet 密文。Fernet
密钥由独立的 `MORPHLAKE_TOKEN_ENCRYPTION_SECRET` 派生，禁止与 pepper、管理员密码共用：

```bash
MORPHLAKE_TOKEN_PEPPER=replace-with-a-long-random-secret
MORPHLAKE_TOKEN_ENCRYPTION_SECRET=replace-with-a-separate-key-encryption-secret
```

升级前只保存摘要的历史 Key 无法逆向恢复，管理页会标记“历史 Key 无法恢复”；点击“重新生成”
后即可查看和复制新 Key，但旧 Key 会立即失效。生产环境必须备份管理数据库、pepper 和加密密钥。

限流使用固定窗口，同时检查请求次数和字节数。0 表示不限制。SQLite 使用
`BEGIN IMMEDIATE`，MySQL/PostgreSQL 使用行锁；超过任一配额返回 429 和 `Retry-After`。

## 传输明细与统计

每次上传、下载和限流拒绝都会记录：Key ID/前缀、业务域、部门、文件、媒体类型、字节数、
耗时、状态、错误码、客户端 IP 和 User-Agent。

API 先把事件与每日汇总写入管理数据库 outbox，保证请求结束时已有可查记录；后台按批次写入
Paimon `multimodal_transfer_audit` 长期表，成功后标记同步。管理库默认保留已同步明细 30 天，
管理页提供天、周、月的条数和数据量统计。同步失败不会丢事件，Prometheus 的
`morphlake_transfer_audit_backlog` 会持续增长并触发运维关注。

outbox 采用“至少一次”写入：若进程在 Paimon 提交成功、管理库标记完成之前退出，恢复后可能
用同一个 `event_id` 重写该事件。审计查询或下游汇总应按 `event_id` 去重；相比“先标记再写”
造成永久丢失，这一取舍更适合审计数据。

## Prometheus

API 和管理服务的 `/metrics` 均要求独立的 `MORPHLAKE_METRICS_TOKEN`。业务 Key、管理密码
均不能代替指标 Key。示例配置：

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
- Key 认证失败和限流拒绝；
- MinIO、Paimon、模型、索引与管理数据库连通状态及数据库类型；
- Paimon 索引维护次数、耗时和最后成功时间；
- Paimon 审计 outbox 积压与同步结果；
- Python 进程 CPU、内存、GC 和运行时指标。

指标标签不包含完整 Key、姓名、手机、文件名或文件 ID，避免隐私泄露和高基数。

## Grafana 与内置监控页

在 Grafana 导入 `monitoring/grafana/morphlake-dashboard.json`，导入时选择 Prometheus 数据源。
面板包含实例状态、API 速率、上传下载流量、P95 时延、依赖连通性、索引状态、审计积压及
进程资源。

设置 `PROMETHEUS_URL` 后，管理页 `http://localhost:8081/admin/monitoring` 会通过 Prometheus
HTTP API 查询关键指标，在 MorphLake 内直接查看当前状态。该页面不会绕过 Prometheus 的
保留策略，也不替代 Grafana 告警。
