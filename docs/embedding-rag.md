# Embedding 与 RAG 知识库

本文记录第一版 Embedding 与 RAG 接口。Embedding 上游使用 OpenAI 兼容的 `POST /embeddings`；知识库元数据、原文、切片和向量保存在 PostgreSQL + pgvector。所有运行时接口通过项目 API Key 自动确定项目，不接受调用方指定 `project_id`。

## 能力边界

- Embedding 支持 OpenAI 兼容纯文本输入；`volc/` 路由前缀另外适配豆包多模态向量接口，可输入文本、图片和视频。多模态输入是网关扩展的结构化格式，不属于 OpenAI 标准 Embeddings 输入结构。
- RAG 首发支持通过 JSON API 写入纯文本文档，按知识库的字符长度和重叠长度切片，逐片生成向量，并用 cosine distance 返回相关片段。返回结果可由业务方拼入 Prompt，再自行请求 LLM。
- 文本、图片/视频来源 URL、记录元数据和向量均按项目知识库保存于 PostgreSQL。媒体原文件由调用方或对象存储管理，网关仅保存媒体 URL 和生成的向量，不接收文件上传或托管媒体文件。
- 知识库创建时固定一个 Embedding 模型。首条数据确定该库向量维度，后续维度必须匹配；切换模型或改变维度时应新建知识库并重新嵌入。
- 当前向量列使用 pgvector `vector` 类型，并以精确 cosine distance 查询；没有 HNSW/IVFFlat 近似索引。适用于初版和规模较小的数据集，规模增长后需基于统一向量维度增加近似索引和召回评估。

## 管理员配置 Embedding 上游

配置环境变量 `LLM_PROVIDER_SECRET_KEY`。该密钥用于加密 LLM、ASR 和 Embedding 上游 API Key。管理员使用控制台登录令牌调用：

控制台入口位于管理员侧栏“Embedding API”。项目 owner/editor 可在项目详情页申请 RAG 服务并进入“管理 RAG 知识库”；owner/editor 可写入或删除知识库数据，viewer 可查看列表但不能写入或发起检索测试。控制台请求使用登录态 token，不需要将个人项目 API Key 暴露给浏览器。

- `GET /api/admin/v1/embedding/providers`
- `POST /api/admin/v1/embedding/providers`
- `PATCH /api/admin/v1/embedding/providers/{provider_id}`
- `DELETE /api/admin/v1/embedding/providers/{provider_id}`
- `POST /api/admin/v1/embedding/providers/{provider_id}/test`

创建连接示例：

```json
{
  "name": "兼容 Embedding 主账号",
  "supplier_name": "供应商名称",
  "route_prefix": "example",
  "base_url": "https://api.example.com/v1",
  "api_key": "供应商密钥"
}
```

调用时 `model` 可填 `example/text-embedding-3-small`，前缀用于选路，斜杠后的名称会原样传给上游。相同前缀的连接按优先级故障切换。密钥只在管理员配置接口可见，项目调用日志不记录输入文本、向量或密钥。

## OpenAI 兼容 Embedding 调用

项目需先开通 `embedding-v1` 并获得 token 额度。API Key 是该项目成员个人创建的 Key：

```bash
curl http://127.0.0.1:8000/v1/embeddings \
  -H "Authorization: Bearer <PROJECT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "example/text-embedding-3-small",
    "input": ["第一段文本", "第二段文本"],
    "encoding_format": "float"
  }'
```

支持 `input` 为一个字符串或字符串数组（最多 64 条、合计 200,000 字符），`dimensions` 可选。当前只支持 `encoding_format=float`。响应遵循 OpenAI Embeddings 的 `object`、`data[].index`、`data[].embedding`、`usage` 结构，并通过 `x-request-id` 返回追踪 ID。月额度按上游 `usage.prompt_tokens` 结算；上游不返回用量时使用字符估算值。

### 火山豆包多模态 Embedding

管理员添加上游时将模型路由前缀设为 `volc`，Base URL 填 `https://ark.cn-beijing.volces.com/api/v3`，测试模型填写方舟模型 ID，例如 `doubao-embedding-vision-251215`。调用方模型为 `volc/doubao-embedding-vision-251215`。网关会把该前缀路由到方舟 `/embeddings/multimodal` 并转换上游响应；火山 API Key 仅保存在网关上游配置中。

多模态输入用结构化 `input` 数组，一个数组代表一个组合内容并返回一个向量。仅文本仍可传 OpenAI 兼容的字符串或字符串数组：

```json
{
  "model": "volc/doubao-embedding-vision-251215",
  "input": [
    {"type": "image_url", "image_url": {"url": "https://cdn.example.com/item.png"}},
    {"type": "text", "text": "蓝色陶瓷杯"}
  ],
  "encoding_format": "float",
  "dimensions": 2048
}
```

视频示例：

```json
{
  "model": "volc/doubao-embedding-vision-251215",
  "input": [
    {"type": "video_url", "video_url": {
      "url": "https://cdn.example.com/demo.mp4",
      "fps": 1,
      "max_video_tokens": 120000
    }}
  ]
}
```

媒体目前须提供可由火山方舟访问的 HTTPS URL；网关不下载或保存原文件。方舟视频格式限 MP4、AVI、MOV，单文件最大 50 MB，且不理解视频音轨。视频抽帧参数由上游模型处理，`fps` 等自定义抽帧选项要求使用 `doubao-embedding-vision-251215` 或更新版本。混合 input 数组会形成一个组合向量；如要每张图片或每段视频独立入库，应在向量数据库 API 中逐记录提交。

## 创建知识库、写入文档和检索

项目需同时开通不计 token 的 `rag-v1` 知识库服务和有独立 token 额度的 `embedding-v1`。知识库创建时固定 Embedding 模型；文档写入和每次检索都会调用 Embedding 服务并消耗对应 token 额度。

如果业务项目自行维护文档和切片，推荐使用 `docs/vector-database.md` 中的 `/v1/vector-stores` API：每次提交一批已经切好的单条文本片段，每条记录分别携带 metadata；网关将同批文本批量嵌入，并在一个数据库事务中写入。标准 OpenAI 兼容上游按数组一次请求；火山多模态接口按单向量请求语义最多并发 8 路。下方 `/v1/rag/.../documents` 是保留的文档级兼容接口，仍会由网关接收整篇文本并按知识库配置切片。

### 创建知识库

`POST /v1/rag/knowledge-bases`

```json
{
  "name": "客服知识库",
  "description": "产品说明和常见问题",
  "model": "example/text-embedding-3-small",
  "chunk_size": 1000,
  "chunk_overlap": 120
}
```

`chunk_size` 使用字符数，范围 100–6000；`chunk_overlap` 必须小于 `chunk_size`。创建后不能更换模型或切片配置，需变更时建议重建知识库并重新导入。

### 写入文本

`POST /v1/rag/knowledge-bases/{knowledge_base_id}/documents`

```json
{
  "title": "退款规则",
  "external_id": "refund-policy-v1",
  "content": "用户可在订单完成后七天内申请退款……",
  "metadata": {"category": "售后", "version": 1}
}
```

单个文档最多 200,000 字符、64 个切片。重复的非空 `external_id` 返回 `409 document_id_conflict`。文档删除时对应向量切片通过外键级联删除。

### 检索

`POST /v1/rag/knowledge-bases/{knowledge_base_id}/search`

```json
{
  "query": "订单多久可以申请退款？",
  "top_k": 5
}
```

响应的 `results` 每项包含文档标题、命中的切片文本、元数据和 cosine 相似度分数。检索只负责找回片段，不会自动调用 LLM。

其他端点：`GET /v1/rag/knowledge-bases` 列出本项目知识库；`GET /v1/rag/knowledge-bases/{id}/documents` 列出文档；`DELETE /v1/rag/knowledge-bases/{id}` 删除知识库；`DELETE /v1/rag/knowledge-bases/{id}/documents/{document_id}` 删除文档。

控制台使用登录态接口 `/api/v1/rag/projects/{project_id}/...`，提供权限查询、知识库列表/创建/删除、文本列表/创建/删除和检索测试。它们复用同一知识库服务逻辑，并验证项目成员角色；Embedding 用量仍按项目 owner 配额和项目 Embedding 额度结算。

## 数据库和部署

迁移 `20261004_0038` 会执行 `CREATE EXTENSION IF NOT EXISTS vector` 并创建上游配置、用量、知识库、文档和向量切片表。数据库服务器必须安装 pgvector 扩展；开发 Compose 使用 `pgvector/pg16`，生产数据库升级前需确认已有 pgvector 可用，再运行 Alembic 升级。

开发环境用量配置：`DEFAULT_EMBEDDING_MONTHLY_TOKEN_LIMIT`（默认 100,000；0 表示新用户不自动获得 Embedding 额度）。管理员密钥加密继续使用 `LLM_PROVIDER_SECRET_KEY`。
