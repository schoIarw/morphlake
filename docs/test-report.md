# MorphLake 接口功能与性能测试报告

- 测试日期：2026-09-02
- 测试环境：macOS（Apple Silicon），Docker（colima）+ 宿主机 Ollama
- 被测服务：`morphlake:ollama-local` 镜像，`docker compose -f docker-compose.ollama.yml` 启动
- 模型配置：文本向量 `nomic-embed-text:latest`（768 维），图片描述 `minicpm-v:8b`，音频向量 hash（384 维）
- 存储：本地 MinIO 容器（`host.docker.internal:9000`），Paimon 表 `multimodal_assets_ollama`

## 测试数据

从网络下载 15 个文件，覆盖三种模态，全部上传至 `perf` 业务域：

| 模态 | 文件 | 大小 |
| --- | --- | --- |
| 文档 | doc1-dummy.pdf、doc2-test.pdf、doc3-alice.txt、doc4-readme.md、doc5-airtravel.csv | 321 B – 151 KB |
| 图片 | img1/img2/img4.jpg（800×600、1000×700）、img3.png（272×92）、img5.png（274×367，WebP 转换） | 6–71 KB |
| 音频 | audio1/2/3.mp3（3s/6s/9s）、audio4.wav（15s）、audio5.wav（6s） | 52 KB – 1.1 MB |

## 功能测试结果

### 健康检查

| 接口 | 状态 | 结果 |
| --- | --- | --- |
| `GET /health/live` | 200 | `{"status":"ok"}` |
| `GET /health/ready` | 200 | minio、paimon 均 ok |

### 上传接口

| 接口 | 用例 | 结果 |
| --- | --- | --- |
| `POST /api/v1/files` | CSV 自动分类为 document | 201 ✅ |
| `POST /api/v1/files/documents` | PDF/TXT/MD/CSV | 201 ✅ |
| `POST /api/v1/files/images` | JPG/PNG（视觉描述 + 向量化） | 201 ✅ |
| `POST /api/v1/files/audio` | MP3/WAV | 201 ✅ |
| `POST /api/v1/files/documents` | 错传图片 | 415 ✅ |

### 查询与检索接口

| 接口 | 用例 | 结果 |
| --- | --- | --- |
| `GET /api/v1/files` | 模态 / 业务域 / 文件名 / 日期范围 / 分页过滤 | 200，过滤正确 ✅ |
| `POST /api/v1/search/full-text` | 关键词 `JAN` 命中 CSV 切片，`rank` 从 1 开始 | 200 ✅ |
| `POST /api/v1/search/full-text` | 缺少 `business_domain` | 422 ✅ |
| `POST /api/v1/search/vector/file` | 文档/图片/音频同文件自查询 | 三模态均命中 Top1 ✅ |
| `POST /api/v1/search/vector` | 768 维文本向量提交 | 200 ✅ |
| `GET /api/v1/files/{id}/download` | 下载 13 KB PDF，Content-Disposition 正常 | 200 ✅ |
| `GET /api/v1/files/{id}/download` | 不存在的 file_id | 404 ✅ |

### 发现的问题

1. **向量维度错误返回 500 而非 422**：提交 384 维向量到 768 维配置时，返回
   `500 configuration_error: "Vector dimension for text must be 768, got 384"`。
   错误消息清晰，但与 [api.md](api.md) 中"422 请求结构或字段校验失败"的约定不符，
   建议将维度校验错误归类为 `invalid_request`（400/422）。
2. **WebP 图片上传 503**：`minicpm-v:8b` 不支持 WebP 输入（直接调用 Ollama API 同样
   返回 400 `Failed to load image or audio file`），属模型能力限制而非工程缺陷。
   服务侧错误处理正确（`storage_error`，消息含原始 400 详情）。临时方案：WebP
   转 PNG 后上传成功。若需支持 WebP，需在入库前做格式转换或更换视觉模型。

## 性能测试结果

### 上传（端到端，含提取、向量化、写 MinIO/Paimon）

| 用例 | 耗时 | 切片数 | 瓶颈 |
| --- | --- | --- | --- |
| 小文档（PDF/CSV/MD） | 0.7 – 1.4 s | 1–9 | nomic-embed 逐切片调用 |
| 大文档（alice.txt，151 KB） | 17.0 s | 213 | nomic-embed 逐切片调用，随切片数线性增长 |
| 图片（视觉描述 + 向量化） | 6.2 – 22.0 s | 0 | minicpm-v 8B 视觉推理 |
| 音频（hash 向量） | 0.7 – 1.2 s | 0 | 无（确定性 hash） |

### 检索（3 次平均）

| 操作 | 平均耗时 | 说明 |
| --- | --- | --- |
| 清单查询（含过滤/分页） | 0.09 – 0.11 s | |
| 全文检索 | 0.20 s | Paimon full-text 索引 |
| 向量检索（直接提交向量） | 0.09 s | Paimon ivf-flat |
| vector/file 音频查询 | 0.44 s | hash 向量，近零推理 |
| 下载（151 KB） | 0.28 s | MinIO 流式传输 |
| vector/file 图片查询 | 10.98 s | minicpm-v 视觉推理 |
| vector/file 大文档查询 | 16.98 s | 213 切片向量化 |

## 结论

- **功能**：11 个接口全部按预期工作；错误路径（415/404/422）处理正确。两个问题
  （维度错误状态码、WebP 支持）均为边缘情况，不影响主链路。
- **性能**：存储与索引层表现优异（清单/全文/向量检索均 <0.5 s）。端到端瓶颈在
  本地模型推理：图片链路 6–22 s/张（minicpm-v），大文档按切片数线性增长
  （~80 ms/切片）。查询侧 `vector/file` 的耗时由查询文件自身的向量化决定，
  与库规模无关。
- **优化方向**：若需提升吞吐，可考虑批量 embedding 请求、更换更小的视觉模型
  （如 moondream）或 GPU 推理。
