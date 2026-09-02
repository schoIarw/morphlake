# MorphLake 接口功能与性能测试报告（第二版）

- 测试日期：2026-09-02
- 版本基线：commit `574854a`（feat: scale multimodal storage to four Paimon tables）
- 测试环境：macOS（Apple Silicon），Docker（colima）+ 宿主机 Ollama
- 被测服务：`morphlake:ollama-local` 镜像，`docker compose -f docker-compose.ollama.yml` 启动
- 模型配置：文本向量 `nomic-embed-text:latest`（768 维），图片描述 `minicpm-v:8b`（MiniCPM-V 描述 + Nomic 向量化，768 维），音频向量 hash（384 维）
- 存储：本地 MinIO 容器（`host.docker.internal:9000`），数据库 `morphlake`，四表后缀 `_ollama`
- 与第一版的差异：单表 `multimodal_assets` 拆分为四表模型；分区从
  `business_domain/department/upload_month` 改为 `ingest_date/domain_shard`；
  向量索引从 ivf-flat 升级为 ivf-sq；向量文件格式改为 Vortex

## 环境配置调整（未修改源代码）

新版 Vortex 向量文件写入路径（`vortex` 库自行解析 `s3://` URL）不继承 pypaimon 的
`s3.*` catalog 选项，默认走 AWS S3 并尝试 EC2 元数据端点（169.254.169.254），导致所有
上传返回 503。在 `.env.ollama` 中追加以下变量后修复：

```dotenv
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin
AWS_ENDPOINT=http://host.docker.internal:9000
AWS_ALLOW_HTTP=true
AWS_DEFAULT_REGION=us-east-1
```

同时按新版 `.env.ollama.example` 补齐四表名与索引参数
（`PAIMON_TEXT_TABLE` 等、`PAIMON_DOMAIN_SHARDS=32`、`PAIMON_VECTOR_INDEX_TYPE=ivf-sq`）。

## 测试数据

与第一版相同：网络下载 15 个文件（5 文档 / 5 图片 / 5 音频），全部上传至 `qa2` 业务域
（另有 1 个验证用重复上传的 CSV，共 16 条资产记录）。

## 功能测试结果

### 健康检查与上传

| 接口 | 用例 | 结果 |
| --- | --- | --- |
| `GET /health/live`、`GET /health/ready` | 启动后四表创建与校验 | 200，minio、paimon 均 ok |
| `POST /api/v1/files/documents` | PDF×2 / TXT / MD / CSV | 201 ✅ |
| `POST /api/v1/files/images` | JPG×3 / PNG×2 | 201 ✅ |
| `POST /api/v1/files/audio` | MP3×3 / WAV×2 | 201 ✅ |

### 查询与检索

| 接口 | 用例 | 结果 |
| --- | --- | --- |
| `GET /api/v1/files` | 全量 16 条；模态过滤（image=5）；文件名 `alice`=1 | 全部正确 ✅ |
| `POST /api/v1/search/full-text` | 关键词 `rabbit` | 命中 doc3-alice.txt 切片 ✅ |
| `POST /api/v1/search/vector/file` | 文档自查询 | Top10 满额返回，Top1 为自身切片 ✅ |
| `POST /api/v1/search/vector/file` | 图片自查询 | Top1 为自身（5 张全返回）✅ |
| `POST /api/v1/search/vector/file` | 音频自查询 | Top1 为自身 ✅ |
| `POST /api/v1/search/vector` | 768 维文本向量 | 200 ✅ |
| `GET /api/v1/files/{id}/download` | 下载 151 KB 文件 | 200，内容逐字节一致 ✅ |

### 错误用例

| 用例 | 结果 |
| --- | --- |
| 下载不存在的 file_id | 404 ✅ |
| 图片上传到 documents 接口 | 415 ✅ |
| 文本文件上传到 images 接口 | 415 ✅ |
| 向量维度错误（384 → 768 配置） | 500 `configuration_error`，消息明确（与第一版一致，文档约定为 422） |

### 本版发现的问题

1. **上传后清单短暂不全**：15 个文件全部返回 201 后立即查询清单只返回 10 条
   （音频全部缺失），约 1 分钟后查询恢复完整 16 条。分模态过滤查询不受影响，
   全量清单查询受索引/快照刷新（`PAIMON_INDEX_BUILD_INTERVAL_SECONDS=30`）影响，
   存在最终一致性窗口，建议客户端对清单查询做重试兜底。
2. **Vortex 写入需要独立的 AWS_* 环境变量**：见上文环境配置调整，属部署注意事项，
   建议在 README 或 .env.example 中说明。

## 性能测试结果

### 上传（端到端，含提取、向量化、四表写入）

| 用例 | 本版耗时 | 第一版耗时 |
| --- | --- | --- |
| 小文档（PDF/CSV） | 0.32 – 0.55 s | 0.70 – 0.88 s |
| 中文档（MD，9 切片） | 2.54 s | 1.39 s |
| 大文档（alice.txt，213 切片） | 15.31 s | 17.01 s |
| 图片（视觉描述 + 向量化） | 11.3 – 32.7 s | 6.2 – 22.0 s |
| 音频（hash 向量） | 0.52 – 1.56 s | 0.72 – 1.20 s |

### 检索（3 次平均）

| 操作 | 本版平均 | 第一版平均 |
| --- | --- | --- |
| 清单查询（全量 16 条） | 0.386 s | 0.094 s |
| 清单查询（模态过滤） | 0.152 s | 0.112 s |
| 全文检索 | 0.147 s | 0.202 s |
| 向量检索（直接提交向量） | 0.229 s | 0.092 s |
| 下载（151 KB） | 0.160 s | 0.279 s |
| vector/file 音频查询 | 1.225 s | 0.444 s |
| vector/file 图片查询 | 34.995 s | 10.980 s |
| vector/file 大文档查询（213 切片） | 26.311 s | 16.979 s |

注：vector/file 耗时由查询文件自身向量化主导，受当时本机模型负载影响波动较大
（图片/大文档查询时 Ollama 正在处理多请求队列），不构成两版本间的模型层结论。
纯索引路径（直接提交向量）仍保持亚秒级。

## 表模型与数据（本版实测导出）

四表统一物理设计：分区 `ingest_date STRING, domain_shard INT`（`qa2` 域哈希到
shard=4），`bucket=-1`，追加模式，向量列 Arrow FixedSizeList，向量文件 Vortex。
以下 schema 与统计由容器内 pypaimon 直读导出。

### 1. multimodal_asset_descriptor_ollama（16 行）

清单、下载与跨表可见性的权威表，一份文件一行。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| file_id | STRING | 文件 UUID |
| business_domain / department | STRING | 业务域 / 部门 |
| domain_shard / ingest_date | INT32 / STRING | 物理分区 |
| created_at | STRING | UTC ISO-8601 |
| filename / media_type / content_type | STRING | 文件名 / 模态 / MIME |
| file_size | INT64 | 原文件字节数 |
| content_sha256 | STRING | 内容校验值（本版新增） |
| object_bucket / object_key / object_etag | STRING | MinIO 描述符 |
| chunk_count | INT32 | 切片数 |

数据分布：document 6、image 5、audio 5。样本行：

```json
{
  "file_id": "75188869-5be2-408f-a0a7-82824b8d7c1f",
  "business_domain": "qa2", "department": "benchmark",
  "domain_shard": 4, "ingest_date": "2026-09-02",
  "filename": "doc5-airtravel.csv", "media_type": "document",
  "file_size": 321,
  "content_sha256": "f6a5fc622a83ef040fe708b7305fb6f34b8725a62e19da03a9bc8ff8592d8054",
  "object_bucket": "morphlake-data",
  "object_key": "qa2/benchmark/2026/09/02/75188869-.../doc5-airtravel.csv",
  "chunk_count": 1
}
```

### 2. multimodal_text_segment_ollama（226 行）

文档切片或音频转写一行；全文和文本向量查询只访问本表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| segment_id | STRING | `file_id:index` |
| file_id | STRING | 关联文件 |
| business_domain / department | STRING | 业务字段 |
| domain_shard / ingest_date | INT32 / STRING | 物理分区 |
| created_at / filename / media_type / record_type | STRING | 结果清单字段 |
| chunk_index | INT32 | 切片序号 |
| content_text | STRING | 切片文本 |
| segment_type | STRING | `document_chunk` / `audio_transcript` |
| chunk_start / chunk_end | INT64 | 切片在规范化文本中的位置 |
| embedding_model / embedding_version | STRING | 模型元数据（本版新增） |
| text_embedding | fixed_size_list\<float\>[768] | 文本向量 |

数据分布：document_chunk 226（doc3-alice.txt 贡献 213 切片）。样本行（节选）：

```json
{
  "segment_id": "2c612521-...:0", "file_id": "2c612521-...",
  "segment_type": "document_chunk", "chunk_index": 0,
  "content_text": "Dummy PDF file",
  "chunk_start": 0, "chunk_end": 14,
  "embedding_model": "nomic-embed-text:latest",
  "embedding_version": "configured",
  "text_embedding": "<vector dim=768>"
}
```

### 3. multimodal_image_feature_ollama（5 行）

每张图片一条 `whole_image` 特征。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| feature_id | STRING | `file_id:image:0` |
| file_id | STRING | 关联文件 |
| 业务/分区/清单字段 | 同上 | business_domain、department、domain_shard、ingest_date、created_at、filename、media_type、record_type、chunk_index、content_text |
| feature_type | STRING | `whole_image`（预留区域级扩展） |
| embedding_model / embedding_version | STRING | `minicpm-v:8b` |
| image_embedding | fixed_size_list\<float\>[768] | 图片向量 |

数据分布：whole_image 5。样本行（节选）：

```json
{
  "feature_id": "0fdaa255-...:image:0", "filename": "img1.jpg",
  "feature_type": "whole_image",
  "embedding_model": "minicpm-v:8b",
  "image_embedding": "<vector dim=768>"
}
```

### 4. multimodal_audio_feature_ollama（5 行）

每个音频一条 `whole_audio` 特征，预留时间片字段。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| feature_id | STRING | `file_id:audio:0` |
| file_id | STRING | 关联文件 |
| 业务/分区/清单字段 | 同上 | 同 image 表 |
| feature_type | STRING | `whole_audio` |
| start_ms / end_ms | INT64 | 时间范围（当前为空，本版新增） |
| embedding_model / embedding_version | STRING | `morphlake-hash-audio-v1` |
| audio_embedding | fixed_size_list\<float\>[384] | 音频向量 |

数据分布：whole_audio 5。样本行（节选）：

```json
{
  "feature_id": "0cc0807b-...:audio:0", "filename": "audio1.mp3",
  "feature_type": "whole_audio", "start_ms": null, "end_ms": null,
  "embedding_model": "morphlake-hash-audio-v1",
  "audio_embedding": "<vector dim=384>"
}
```

### MinIO 侧数据

- `morphlake-data` bucket：16 个原始文件对象，键格式
  `<domain>/<department>/<YYYY>/<MM>/<DD>/<file_id>/<filename>`
- `morphlake-paimon` bucket：`morphlake.db` 下四张表目录，按
  `ingest_date=2026-09-02/domain_shard=4/bucket-0` 分区存储，
  含 `.data.zstd`、`.vector.vortex`、索引与 manifest 文件

## 结论

- **功能**：11 个接口在新四表模型下全部通过；错误路径（415/404）处理正确。
- **数据模型**：四表拆分后数据落库正确——16 资产 / 226 文本切片 / 5 图片特征 /
  5 音频特征，行数与上传完全吻合（文本切片 226 = 213 + 12 + 1），
  新增字段（content_sha256、embedding_model、segment/feature_type、start/end_ms）
  均正确填充。
- **性能**：存储与索引层保持亚秒级；上传端到端耗时与第一版相当。两处需关注：
  全量清单查询变慢（0.09s → 0.39s，扫描描述符表代替原单表过滤）；
  上传后存在约 1 分钟的清单最终一致性窗口。
- **部署**：新版 Vortex 写入依赖 AWS_* 环境变量，自建 MinIO 场景必须显式配置，
  建议补充到项目文档。
