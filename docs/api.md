# API 使用说明

基础地址示例：`http://localhost:8080`。如设置了 `MORPHLAKE_API_KEY`，除健康检查外的请求
都必须携带 `X-API-Key`。完整机器可读契约位于运行时 `/openapi.json`。

所有错误使用固定结构：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Request validation failed",
    "details": []
  }
}
```

## 健康检查

```bash
curl http://localhost:8080/health/live
curl http://localhost:8080/health/ready
```

`live` 只表示进程可响应；`ready` 会检查 MinIO 和 Paimon。

## 上传

`POST /api/v1/files`，`multipart/form-data`。

```bash
curl -X POST http://localhost:8080/api/v1/files \
  -H 'X-API-Key: change-me' \
  -F 'business_domain=finance' \
  -F 'department=treasury' \
  -F 'file=@./liquidity-report.docx'
```

成功返回 201：

```json
{
  "file_id": "1d8b9f47-20fe-4ef5-a37c-3023074cc758",
  "filename": "liquidity-report.docx",
  "media_type": "document",
  "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "file_size": 12034,
  "business_domain": "finance",
  "department": "treasury",
  "created_at": "2026-08-30T03:00:00Z",
  "object_bucket": "morphlake-data",
  "object_key": "finance/treasury/2026/08/30/.../liquidity-report.docx",
  "object_etag": "...",
  "chunk_count": 8
}
```

## 清单查询

`GET /api/v1/files`。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `media_type` | string | document / image / audio |
| `business_domain` | string | 业务域精确匹配 |
| `department` | string | 部门精确匹配 |
| `filename` | string | 文件名包含匹配 |
| `start_date` | date | 起始日期（含） |
| `end_date` | date | 结束日期（含） |
| `limit` | int | 1–200，默认 50 |
| `offset` | int | 默认 0 |

```bash
curl -G http://localhost:8080/api/v1/files \
  -H 'X-API-Key: change-me' \
  --data-urlencode 'business_domain=finance' \
  --data-urlencode 'media_type=document' \
  --data-urlencode 'filename=liquidity' \
  --data-urlencode 'start_date=2026-08-01' \
  --data-urlencode 'end_date=2026-08-31' \
  --data-urlencode 'limit=50'
```

## 全文检索

`POST /api/v1/search/full-text`。

```bash
curl -X POST http://localhost:8080/api/v1/search/full-text \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{
    "business_domain": "finance",
    "keyword": "liquidity coverage ratio",
    "start_date": "2026-08-01",
    "end_date": "2026-08-31",
    "limit": 20
  }'
```

返回按 Paimon 搜索顺序排列的文件/切片命中列表，`rank` 从 1 开始。

## 向量检索

`POST /api/v1/search/vector`。

```bash
curl -X POST http://localhost:8080/api/v1/search/vector \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{
    "business_domain": "finance",
    "vector_field": "image",
    "vector": [0.01, -0.02],
    "start_date": "2026-08-01",
    "end_date": "2026-08-31",
    "limit": 10
  }'
```

示例数组为简写；实际维度必须严格等于所选模态配置。`vector_field` 可为 `text`、`image`
或 `audio`。

## 下载

`GET /api/v1/files/{file_id}/download`。

```bash
curl -L -OJ \
  -H 'X-API-Key: change-me' \
  http://localhost:8080/api/v1/files/1d8b9f47-20fe-4ef5-a37c-3023074cc758/download
```

响应包含 `Content-Disposition`、`Content-Length` 和 `ETag`，文件内容从 MinIO 流式传输。

## 常见状态码

| 状态码 | 含义 |
| --- | --- |
| 201 | 上传成功 |
| 400 | 业务参数错误 |
| 401 | API key 缺失或错误 |
| 404 | 文件不存在 |
| 413 | 超过上传大小限制 |
| 415 | 文件类型不支持 |
| 422 | 请求结构或字段校验失败 |
| 503 | MinIO、模型或 Paimon 不可用 |
