# Paimon 后端表模型

默认标识：`morphlake.multimodal_assets`。

一份文件对应一条 `record_type=file` 记录；文档的每个切片对应一条
`record_type=chunk` 记录。它们共用 `file_id` 和 MinIO 描述符，`row_id` 唯一。表无主键，
采用 append-only 模式。

| 字段 | Arrow/Paimon 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `row_id` | STRING | 否 | 文件行为 UUID；切片行为 `file_id:index` |
| `file_id` | STRING | 否 | 文件逻辑 ID |
| `record_type` | STRING | 否 | `file` 或 `chunk` |
| `business_domain` | STRING | 否 | 业务域，一级分区 |
| `department` | STRING | 否 | 业务部门，二级分区 |
| `upload_month` | STRING | 否 | `YYYY-MM`，三级分区 |
| `created_at` | STRING | 否 | UTC ISO-8601 上传时间 |
| `filename` | STRING | 否 | 原始文件名（移除路径） |
| `media_type` | STRING | 否 | `document` / `image` / `audio` |
| `content_type` | STRING | 否 | MIME type |
| `file_size` | BIGINT | 否 | 原始文件字节数 |
| `object_bucket` | STRING | 否 | MinIO 数据 bucket |
| `object_key` | STRING | 否 | MinIO 对象键 |
| `object_etag` | STRING | 是 | MinIO ETag |
| `chunk_count` | INT | 否 | 文件切片总数 |
| `chunk_index` | INT | 是 | 切片序号 |
| `chunk_start` | BIGINT | 是 | 切片在规范化文本中的起始字符位置 |
| `chunk_end` | BIGINT | 是 | 结束字符位置（不含） |
| `content_text` | STRING | 是 | 切片文本、图片文件名或音频转写文本 |
| `text_embedding` | ARRAY<FLOAT> | 是 | 文档切片或音频转写向量 |
| `image_embedding` | ARRAY<FLOAT> | 是 | 图片文件向量 |
| `audio_embedding` | ARRAY<FLOAT> | 是 | 音频文件向量 |

## 表参数

```properties
bucket=-1
row-tracking.enabled=true
data-evolution.enabled=true
deletion-vectors.enabled=false
blob-as-descriptor=true
global-index.search-mode=full
```

虽然没有 Paimon BLOB 列，`blob-as-descriptor=true` 仍作为显式防护配置；工程从不把原始
二进制传给表写入器。

## 全局索引

| 列 | 类型 | 目的 |
| --- | --- | --- |
| `file_id` | `btree` | 文件详情与下载定位 |
| `content_text` | `full-text` | 文档切片及转写文本搜索 |
| `text_embedding` | `ivf-flat` | 文本向量搜索 |
| `image_embedding` | `ivf-flat` | 图片向量搜索 |
| `audio_embedding` | `ivf-flat` | 音频向量搜索 |

向量维度来自环境变量，必须同时与模型 YAML 保持一致。服务在每次数据提交后调用 PyPaimon
的增量全局索引构建；已覆盖 row range 会被跳过。

## MinIO 对象键

```text
<business_domain>/<department>/<YYYY>/<MM>/<DD>/<file_id>/<safe_filename>
```

文件名、业务域和部门会移除路径与不安全字符；`file_id` 保证对象键唯一。
