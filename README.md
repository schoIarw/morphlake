# MorphLake

MorphLake 是一个以 **Apache Paimon 2.0 + MinIO** 为核心的多模态数据底座。它用一个
Python/FastAPI 容器提供上传、清单查询、全文检索、向量检索和下载接口，不引入 Spark、
Milvus、Elasticsearch，也不依赖常驻 Flink 作业。

> 当前状态：可运行的首个版本。生产部署前需要接入实际 MinIO 地址和模型网关，并根据
> 数据量完成压测与备份策略验证。

## 设计目标

- **简单**：服务、写入和索引构建都在一个 Python 容器内完成。
- **稳定**：四张固定粒度的追加型 Paimon 表、单进程写锁；不维护常驻计算作业。
- **Descriptor‑Only**：文件二进制只存 MinIO；Paimon 保存对象引用、元数据、文本和向量。
- **原生检索**：全文使用 Paimon `full-text`，向量默认使用 Paimon `ivf-sq` 全局索引。
- **配置驱动模型**：模型提供方、地址、模型名、维度、超时均在 YAML/环境变量中配置。

```mermaid
flowchart TB
    C["用户或应用"] --> A["MorphLake FastAPI<br/>单容器"]
    A --> M["MinIO<br/>原始文件"]
    A --> P["Paimon 2.0<br/>描述符、切片、向量、索引"]
    A --> G["配置化模型 API<br/>向量与语音转写"]
    P --> W["MinIO<br/>Paimon warehouse"]
```

## 能力范围

| 能力 | 支持内容 |
| --- | --- |
| 上传 | 文档、图片、音频独立接口；另保留自动分类兼容接口 |
| 必填元数据 | `business_domain`（业务域）、`department`（业务部门） |
| 文档处理 | 文本提取、可配置重叠切片、逐切片向量化 |
| 图片处理 | 文件级图片向量 |
| 音频处理 | 文件级音频向量；可选语音转写和转写文本向量 |
| 清单查询 | 类型、日期、业务域、部门、文件名关键字 |
| 全文检索 | 业务域、日期范围、全文关键字 |
| 向量检索 | 上传文档/图片/音频自动向量化并返回 Paimon Top10；也支持直接提交向量 |
| 下载 | 按 `file_id` 流式下载 MinIO 对象 |

旧式二进制 `.doc` 会返回 415；请先转换为 `.docx`。扫描版 PDF 的 OCR 不在默认链路中，
可通过模型网关扩展。

## 快速开始

### 1. 配置

```bash
cp .env.example .env
```

至少修改以下配置：

```dotenv
MINIO_ENDPOINT=minio.example.internal:9000
MINIO_ACCESS_KEY=replace-me
MINIO_SECRET_KEY=replace-me
MINIO_DATA_BUCKET=morphlake-data
MINIO_PAIMON_BUCKET=morphlake-paimon
PAIMON_WAREHOUSE=s3://morphlake-paimon/warehouse
```

开发配置默认使用确定性的 `hash` 向量，仅用于接口和索引冒烟验证，不具备语义效果。
生产环境应修改 `config/models.yaml`，把对应 `provider` 改为
`openai_compatible`，并设置 `EMBEDDING_API_BASE`、`EMBEDDING_API_KEY` 和模型名称。

### MacBook + Ollama 本地测试

工程提供独立的 `config/models.ollama.yaml`、`.env.ollama.example` 和
`docker-compose.ollama.yml`，不会改变默认或生产模型配置。模型映射如下：

| 数据类型 | Ollama 链路 | 向量维度 |
| --- | --- | --- |
| 文档 | `nomic-embed-text:latest` | 768 |
| 图片 | `minicpm-v:8b` 生成检索描述，再由 `nomic-embed-text:latest` 向量化 | 768 |
| 音频 | 本地测试使用确定性 hash；当前所列 Ollama 模型没有音频语义嵌入能力 | 384 |

你当前列出的模型已经包含本配置所需的两个模型。新机器可用以下命令下载或校验：

```bash
ollama pull nomic-embed-text:latest
ollama pull minicpm-v:8b
ollama list
```

Ollama macOS 应用通常会自动启动服务；若没有运行，可执行：

```bash
ollama serve
```

先在 Mac 终端验证文本向量和视觉模型：

```bash
curl http://localhost:11434/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"nomic-embed-text:latest","input":"多模态数据底座测试"}'

IMAGE_BASE64=$(base64 < ./test.png | tr -d '\n')
curl http://localhost:11434/api/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "model":"minicpm-v:8b",
    "messages":[{
      "role":"user",
      "content":"请描述图片中的对象和文字",
      "images":["'"$IMAGE_BASE64"'"]
    }],
    "stream":false
  }'
```

启动 MorphLake 前复制本地配置，并填写可访问的 MinIO 地址和凭据：

```bash
cp .env.ollama.example .env.ollama
docker compose -f docker-compose.ollama.yml up --build -d
curl http://localhost:8080/health/ready
```

容器通过 Docker Desktop 的 `host.docker.internal:11434` 访问 Mac 上的 Ollama。本配置使用
一套带 `_ollama` 后缀的四张 Paimon 表，避免与默认 384/512 维索引混用。不要在已有 Paimon
向量表上直接修改维度。

### 2. 启动单容器

```bash
docker compose up --build -d
curl http://localhost:8080/health/ready
```

容器启动时会：

1. 检查并创建两个 MinIO bucket（需要账号具有相应权限）；
2. 自动创建资产描述符、文本切片、图片特征和音频特征四张 Paimon 表；
3. 校验已存在表的字段、`ingest_date/domain_shard` 分区、`bucket=-1` 和 deletion-vector；
4. 启动定时增量索引维护；上传请求本身不重建索引。

### 3. 上传并查询

```bash
curl -X POST http://localhost:8080/api/v1/files/documents \
  -H 'X-API-Key: replace-if-configured' \
  -F 'business_domain=risk' \
  -F 'department=compliance' \
  -F 'file=@./contract.pdf'
```

用同模态查询文件自动向量化并查询 Top10（查询文件不会入库）：

```bash
curl -X POST http://localhost:8080/api/v1/search/vector/file \
  -H 'X-API-Key: replace-if-configured' \
  -F 'business_domain=risk' \
  -F 'start_date=2026-01-01' \
  -F 'end_date=2026-12-31' \
  -F 'file=@./query.pdf'
```

```bash
curl -G http://localhost:8080/api/v1/files \
  -H 'X-API-Key: replace-if-configured' \
  --data-urlencode 'media_type=document' \
  --data-urlencode 'business_domain=risk' \
  --data-urlencode 'department=compliance' \
  --data-urlencode 'filename=contract' \
  --data-urlencode 'start_date=2026-01-01' \
  --data-urlencode 'end_date=2026-12-31'
```

```bash
curl -X POST http://localhost:8080/api/v1/search/full-text \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: replace-if-configured' \
  -d '{
    "business_domain": "risk",
    "keyword": "counterparty exposure",
    "start_date": "2026-01-01",
    "end_date": "2026-12-31",
    "limit": 20
  }'
```

向量请求中的维度必须与配置一致（默认文本 384、图片 512、音频 384）：

```bash
python - <<'PY' > /tmp/vector-request.json
import json
print(json.dumps({
    "business_domain": "risk",
    "vector_field": "text",
    "vector": [0.0] * 384,
    "start_date": "2026-01-01",
    "end_date": "2026-12-31",
    "limit": 10,
}))
PY

curl -X POST http://localhost:8080/api/v1/search/vector \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: replace-if-configured' \
  --data-binary @/tmp/vector-request.json
```

```bash
curl -OJ \
  -H 'X-API-Key: replace-if-configured' \
  http://localhost:8080/api/v1/files/FILE_ID/download
```

完整接口说明见 [docs/api.md](docs/api.md)，表结构见
[docs/table-model.md](docs/table-model.md)，架构和生产注意事项见
[docs/architecture.md](docs/architecture.md)。启动后也可访问 `/docs` 查看 OpenAPI UI。

## 海量数据分区与 unaware bucket

四表统一按 `ingest_date / domain_shard` 分区，并设置 `bucket=-1`。`domain_shard` 是业务域的
稳定哈希模 32；部门、业务域和媒体类型使用 Bitmap 索引，不按“部门 × 模态 × 日期”动态
分表。这样在每天 1 亿条、十年约 3650 亿条的规划下，表数量仍固定，日期裁剪与索引分片也
可独立维护。PyPaimon 2.0 的通用全文和向量全局索引要求 unaware bucket，固定桶会被拒绝。

通用全局索引同时要求关闭 deletion vectors，因此表采用追加模式。首个版本不提供删除/更新
接口；如后续需要数据撤回，应设计软删除字段和定期重写流程，而不是破坏当前索引约束。

## 开发验证

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff format --check .
ruff check .
pytest -q
```

带原生 Paimon 全文和向量索引的集成测试使用本地临时 warehouse，不连接真实 MinIO。
Docker 镜像构建也包含在 GitHub Actions 中。

## 目录

```text
config/                 模型和切片配置
docs/                   架构、表模型、接口文档
src/morphlake/api.py    固定 API 契约
src/morphlake/services  MinIO、模型、Paimon 和业务编排
tests/                  单元、接口和 PyPaimon 集成测试
```

## License

Apache-2.0
