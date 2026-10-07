# 向量数据库服务

网关提供项目隔离的向量数据库 HTTP API。调用方只需要项目 API Key、网关 URL、collection 名称和 Embedding 模型，不需要访问 PostgreSQL。

## API

```text
POST   /v1/vector-stores/collections
GET    /v1/vector-stores/collections
DELETE /v1/vector-stores/collections/{collection}
POST   /v1/vector-stores/collections/{collection}/upsert
POST   /v1/vector-stores/collections/{collection}/delete
POST   /v1/vector-stores/collections/{collection}/query
```

所有请求使用：

```text
Authorization: Bearer <项目 API Key>
```

创建集合：

```json
{
  "name": "customer_service",
  "model": "openai/text-embedding-3-small",
  "chunk_size": 1000
}
```

批量写入已切分的向量记录。切片由业务项目负责；每一项对应一条独立向量，metadata 只附着在这一条记录上。网关不会把多项再次合并或拆分，且每段文本不能超过集合的 `chunk_size`：

```json
{
  "vectors": [
    {"id": "refund-v1-001", "content": "退款规则的第一段……", "metadata": {"category": "售后", "source": "refund-v1"}},
    {"id": "refund-v1-002", "content": "退款规则的第二段……", "metadata": {"category": "售后", "source": "refund-v1"}}
  ]
}
```

`id` 是调用方提供的稳定记录 ID；相同 ID 再次写入会替换该记录。每批最多 64 条，批次内 ID 必须唯一。网关仍会为每条文本生成 Embedding，并写入 PostgreSQL + pgvector；不会替业务项目负责文档切分。旧客户端可以继续使用 `documents` 字段名，其内容也按单条已切片记录处理。

查询：

```json
{
  "query": "多久可以退款？",
  "top_k": 5,
  "metadata": {"category": "售后"}
}
```

集合创建后固定 Embedding 模型和向量维度。写入和查询会调用 Embedding 服务并记录项目服务调用、额度和 Trace ID；数据库只保存脱敏的请求元数据，不保存 API Key 明文。`chunk_overlap` 为兼容集合管理接口和旧 `/v1/rag/.../documents` 文档接口保留；向量数据库 API 的预切片写入不会使用它。

## Python 客户端

可直接复制 [clients/gateway_vector_store.py](../clients/gateway_vector_store.py)，它只使用 Python 标准库：

```python
from gateway_vector_store import GatewayVectorStore

store = GatewayVectorStore(
    base_url="https://gateway.example.com/gateway/v1",
    api_key="<项目 API Key>",
    collection="customer_service",
)
store.create_collection("openai/text-embedding-3-small")
store.add_vectors([
    {"id": "faq-1-001", "content": "退款规则的一个已切分片段……", "metadata": {"category": "售后", "source": "faq-1"}},
    {"id": "faq-1-002", "content": "退款规则的另一个已切分片段……", "metadata": {"category": "售后", "source": "faq-1"}}
])
hits = store.similarity_search("如何退款？", k=3, metadata={"category": "售后"})
```

`add_documents()` 暂时作为兼容旧客户端代码的方法名保留，但参数语义相同：传入的每项必须已经是一个文本片段。业务项目可自行基于标题、段落或自定义分隔符切片，并为每条切片写入独立 metadata。
