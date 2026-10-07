"""Embedding 服务目录定义。"""

from app.domain.service_catalog import ServiceCatalogItem

EMBEDDING_SERVICE = ServiceCatalogItem(
    code="embedding-v1",
    name="Embedding 服务",
    models=(),
    quota_unit="tokens",
)

RAG_SERVICE = ServiceCatalogItem(
    code="rag-v1",
    name="向量数据库服务",
    models=(),
    quota_unit=None,
)
