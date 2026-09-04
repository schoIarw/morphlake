# Paimon 后端表模型

API 服务首次启动时使用 `ignore_if_exists=true` 创建以下五张表，并校验必需字段、统一分区、
`bucket=-1` 和 deletion vector 配置。已存在但与模型不兼容的表会令启动失败，避免静默写坏。

## 统一物理设计

| 配置 | 值 |
| --- | --- |
| 分区 | `ingest_date STRING, domain_shard INT` |
| 业务分片 | `SHA-256(business_domain)[0:8] % PAIMON_DOMAIN_SHARDS`，默认 32 |
| 桶 | `bucket=-1` |
| 行跟踪 / 数据演进 | 开启 |
| 删除向量 | 关闭 |
| 向量列 | Arrow FixedSizeList，对应 Paimon `VECTOR<FLOAT,n>` |
| 向量文件 | Vortex，目标 1 GiB |
| 普通文件 | Zstd，目标 512 MiB |
| 索引分片 | 默认每 500,000 行 |

`department` 不作为分区键：部门基数和组织结构会变化，放入目录会造成大量小分区；它在每张
业务表上由 Bitmap 索引过滤。每日分区便于十年数据的时间裁剪和生命周期管理，32 个稳定
业务域分片用于控制单分区峰值。

## 1. multimodal_asset_descriptor

一份 MinIO 文件一行，是清单、下载和跨表可见性的权威表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| file_id | STRING | 文件 UUID、BTree 索引 |
| business_domain / department | STRING | 业务字段、Bitmap 索引 |
| domain_shard / ingest_date | INT / STRING | 物理分区 |
| created_at | STRING | UTC ISO-8601 时间 |
| filename / media_type / content_type | STRING | 文件名、模态、MIME |
| file_size | BIGINT | 原文件字节数 |
| content_sha256 | STRING | 内容校验值 |
| object_bucket / object_key / object_etag | STRING | MinIO 描述符 |
| chunk_count | INT | 对应文本切片或转写段数量 |

默认表名由 `PAIMON_TABLE` 配置。

## 2. multimodal_text_segment

文档切片或音频转写一行；全文和文本向量查询只访问本表。

| 字段组 | 主要字段 |
| --- | --- |
| 标识 | segment_id, file_id, segment_type |
| 业务/分区 | business_domain, department, domain_shard, ingest_date |
| 结果清单 | created_at, filename, media_type, record_type, chunk_index |
| 内容 | content_text, chunk_start, chunk_end |
| 模型 | embedding_model, embedding_version |
| 向量 | `text_embedding VECTOR<FLOAT,text_dimension>` |

索引：file_id BTree，业务域/部门/类型/segment_type Bitmap，content_text Full-Text，向量 IVF-SQ。

## 3. multimodal_image_feature

当前每张图片一条 `whole_image` 特征，后续可追加区域或页面级 feature，而无需改变资产表。
主要字段为 feature_id、file_id、公共业务/分区/结果字段、feature_type、模型元数据及
`image_embedding VECTOR<FLOAT,image_dimension>`。索引包括 file_id BTree、业务字段 Bitmap
和向量 IVF-SQ。

MacBook Ollama 配置使用 MiniCPM-V 生成图片描述，再以 Nomic 对描述向量化，图片向量因此
为固定 768 维。

## 4. multimodal_audio_feature

当前每个音频一条 `whole_audio` 特征，预留 start_ms/end_ms 以支持后续时间片。主要字段为
feature_id、file_id、公共业务/分区/结果字段、feature_type、时间范围、模型元数据及
`audio_embedding VECTOR<FLOAT,audio_dimension>`。若模型返回转写，还会在文本表写一条
`audio_transcript`，从而进入全文和文本向量检索。

## 5. multimodal_transfer_audit

一条上传、下载或限流拒绝事件一行。SQLite 作为实时 outbox 和天/周/月汇总缓存；API 后台
将 outbox 批量追加到本表，形成长期审计明细。

| 字段组 | 主要字段 |
| --- | --- |
| 标识 | event_id, token_id, token_prefix, file_id |
| 业务/分区 | business_domain, department, domain_shard, ingest_date |
| 传输 | operation, occurred_at, filename, media_type, byte_count, duration_ms |
| 结果 | status, error_code |
| 客户端 | client_ip, user_agent |

索引：event_id、token_id、file_id BTree；业务域、部门、操作和状态 Bitmap。表不保存 Token
明文、使用人姓名和手机号码；人员信息只保留在受管理权限保护的 SQLite 中。

## 表参数

```properties
bucket=-1
row-tracking.enabled=true
data-evolution.enabled=true
deletion-vectors.enabled=false
blob-as-descriptor=true
vector.file.format=vortex
vector-index.search-mode=fast
full-text-index.search-mode=fast
scalar-index.search-mode=fast
global-index.row-count-per-shard=500000
```

向量维度、表名、业务域分片数和索引类型均来自环境变量。模型 YAML 中的维度必须与 Paimon
配置一致，服务在启动时校验。已有固定维向量表不能直接切换模型维度，应创建一套新表并迁移。

## MinIO 对象键

```text
<business_domain>/<department>/<YYYY>/<MM>/<DD>/<file_id>/<safe_filename>
```

对象键包含业务路径便于运维定位，但它不是 Paimon 分区模型的一部分。
