# MorphLake API

业务 API 默认地址为 `http://localhost:8080`。除 `/health/live` 外，所有业务接口都必须携带
管理台分配的 API Key：

```http
Authorization: Bearer mlk_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

兼容客户端也可使用 `X-API-Token` 或 `X-API-Key`。上传和查询请求不再提交业务域、部门：

- 业务域 Key 上传时自动使用其绑定的 `business_domain + department`，查询时仅能访问所属业务域；
- 默认管理 Key 的业务域、部门均为“管理员”，查询时不添加业务范围过滤，可查询全部数据；
- 下载接口会读取文件描述符并校验业务域，业务域 Key 不能下载其他业务域文件。

## 错误格式与 Key 状态

```json
{"error":{"code":"token_disabled","message":"API token is disabled"}}
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 401 | `token_required` | 未提交 Key |
| 401 | `token_invalid` | Key 不存在或密钥不正确 |
| 401 | `token_invalid_scheme` | Authorization 不是 Bearer |
| 403 | `token_disabled` | Key 已停用 |
| 403 | `token_deleted` | Key 已删除 |
| 403 | `token_expired` | Key 已过期 |
| 403 | `token_scope_mismatch` | 业务域 Key 访问了其他业务域文件 |
| 429 | `rate_limit_exceeded` | 上传/下载周期次数或字节配额耗尽 |

429 响应包含 `Retry-After` 秒数。限流同时检查周期请求次数和周期字节数；配置值 0 表示不限制。

## 健康检查

```bash
curl http://localhost:8080/health/live
curl http://localhost:8080/health/ready \
  -H "Authorization: Bearer $MORPHLAKE_TOKEN"
```

`live` 只表示 API 进程可响应。`ready` 检查 MinIO、Paimon、模型端点以及最近一次索引维护状态；
依赖失败时返回 503。

## 上传

通用上传接口会按扩展名识别文档、图片或音频；类型专用接口会额外验证文件类型。

| 类型 | 路径 |
| --- | --- |
| 自动识别 | `POST /api/v1/files` |
| 文档 | `POST /api/v1/files/documents` |
| 图片 | `POST /api/v1/files/images` |
| 音频 | `POST /api/v1/files/audio` |

```bash
curl -X POST http://localhost:8080/api/v1/files/documents \
  -H "Authorization: Bearer $MORPHLAKE_TOKEN" \
  -F 'file=@./report.pdf'
```

上传成功返回 201 和资产描述符。业务域、部门来自 Key；原始二进制写入 MinIO，Paimon 只保存
描述符、切片和向量。

## 清单查询

```bash
curl -G http://localhost:8080/api/v1/files \
  -H "Authorization: Bearer $MORPHLAKE_TOKEN" \
  --data-urlencode 'media_type=document' \
  --data-urlencode 'filename=report' \
  --data-urlencode 'start_date=2026-01-01' \
  --data-urlencode 'end_date=2026-12-31' \
  --data-urlencode 'limit=50' \
  --data-urlencode 'offset=0'
```

支持类型、文件名关键字和闭区间日期过滤；结果按 `created_at/file_id` 倒序。业务范围由 Key
自动限定，不接受业务域和部门查询字段。

## 全文检索

```bash
curl -X POST http://localhost:8080/api/v1/search/full-text \
  -H "Authorization: Bearer $MORPHLAKE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "keyword":"counterparty exposure",
    "start_date":"2026-01-01",
    "end_date":"2026-12-31",
    "limit":20
  }'
```

使用 Paimon `full-text` 原生全局索引，返回文件或切片命中清单。业务范围由 Key 自动限定。

## 直接向量检索

向量维度必须与对应模型及 Paimon 表一致。

```bash
curl -X POST http://localhost:8080/api/v1/search/vector \
  -H "Authorization: Bearer $MORPHLAKE_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @vector-request.json
```

`vector-request.json`：

```json
{
  "vector_field": "text",
  "vector": [0.01, 0.02],
  "start_date": "2026-01-01",
  "end_date": "2026-12-31",
  "limit": 10
}
```

示例向量仅展示结构，必须替换为完整维度。结果按 Paimon 相似度分数降序恢复并生成 `rank`。

## 上传查询文件并返回 Top10

查询文件只用于向量化，不会写入 MinIO 或 Paimon。

```bash
curl -X POST http://localhost:8080/api/v1/search/vector/file \
  -H "Authorization: Bearer $MORPHLAKE_TOKEN" \
  -F 'start_date=2026-01-01' \
  -F 'end_date=2026-12-31' \
  -F 'file=@./query.png'
```

文档提取和切片后取归一化平均向量；图片调用视觉描述再做文本向量；音频使用配置的音频向量
链路。固定返回 Key 权限范围内的同模态 Top10。

## 下载

```bash
curl -OJ \
  -H "Authorization: Bearer $MORPHLAKE_TOKEN" \
  http://localhost:8080/api/v1/files/FILE_ID/download
```

下载前验证资产所属业务域，并按 Key 的下载次数及字节配额限流。管理 Key 可下载任意业务域文件。

## Prometheus 与 OpenAPI

```bash
curl http://localhost:8080/metrics \
  -H "Authorization: Bearer $MORPHLAKE_METRICS_TOKEN"
```

指标 Key 与业务 Key 相互独立。`/docs` 和 `/openapi.json` 使用管理账号 HTTP Basic 认证。
管理服务运行在 8081，不承载任何 `/api/v1` 业务接口。
