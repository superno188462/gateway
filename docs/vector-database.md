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

批量写入已准备好的向量记录。文本切片由业务项目负责；图片和视频以一条媒体 URL 记录为单位。每一项对应一条独立向量，metadata 只附着在这一条记录上。网关不会把文本切片或媒体拆分；文本不能超过集合的 `chunk_size`：

```json
{
  "vectors": [
    {"id": "refund-v1-001", "content": "退款规则的第一段……", "metadata": {"category": "售后", "source": "refund-v1"}},
    {"id": "refund-v1-002", "content": "退款规则的第二段……", "metadata": {"category": "售后", "source": "refund-v1"}}
  ]
}
```

图片记录使用 `modality: image` 和可访问的 HTTPS `media_url`；可选 `content` 作为图片说明，并与图片一起生成一个组合向量：

```json
{
  "vectors": [
    {
      "id": "product-42-image-1",
      "modality": "image",
      "media_url": "https://cdn.example.com/product-42.png",
      "content": "蓝色陶瓷杯，容量 350 毫升",
      "metadata": {"category": "商品", "product_id": "42"}
    }
  ]
}
```

视频将 `modality` 改为 `video`，`media_url` 需指向 MP4、AVI 或 MOV。每条记录保存一个整体视频向量，抽帧由豆包模型完成；视频 URL 保存在该记录的 `metadata.media_url`。可使用 `video_options` 传入 `fps`、`max_video_tokens` 等抽帧参数；未指定 `max_video_tokens` 时，网关按方舟接口上限 204800 tokens 预留项目额度。原媒体必须可被火山方舟访问，推荐存放在业务方的对象存储。需要启用火山豆包时，管理员连接前缀设为 `volc`，集合模型使用 `volc/doubao-embedding-vision-251215`。图片和视频记录暂只适配该火山上游；文本模型继续使用 OpenAI 兼容上游。

`id` 是调用方提供的稳定记录 ID；相同 ID 再次写入会替换该记录。每批最多 64 条，批次内 ID 必须唯一。网关把同批文本合并成一次批量 Embedding 调用，然后在一个数据库事务中替换/写入整批向量；Embedding 失败或数据库写入失败时，已有记录不会被提前删除。标准 OpenAI 兼容上游使用单次数组请求。火山多模态 Embedding 的接口一次返回一条向量，因此同批文本会拆成独立上游请求，并由网关限制为最多 8 路并发。旧客户端可以继续使用 `documents` 字段名，其内容也按单条已切片记录处理。

查询：

```json
{
  "query": "多久可以退款？",
  "top_k": 5,
  "metadata": {"category": "售后"}
}
```

查询也可使用图片或视频作为 query，例如 `{"query":[{"type":"image_url","image_url":{"url":"https://cdn.example.com/query.png"}}],"top_k":5}`。文本、图片和视频共享同一集合向量空间时，查询可跨模态返回结果；metadata 过滤照常生效。

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
    {"id": "faq-1-002", "content": "退款规则的另一个已切分片段……", "metadata": {"category": "售后", "source": "faq-1"}},
    {"id": "product-42-image-1", "modality": "image", "media_url": "https://cdn.example.com/product-42.png", "metadata": {"category": "商品"}}
])
hits = store.similarity_search("如何退款？", k=3, metadata={"category": "售后"})
```

`add_documents()` 暂时作为兼容旧客户端代码的方法名保留，但参数语义相同：传入的每项必须已经是一个文本片段。业务项目可自行基于标题、段落或自定义分隔符切片，并为每条切片写入独立 metadata。
