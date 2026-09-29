"""LLM 模块对外发布的服务目录定义。"""

from app.domain.service_catalog import ServiceCatalogItem

LLM_SERVICE = ServiceCatalogItem(
    code="mock-llm-v1",
    name="LLM 服务",
    models=("mock-chat",),
)
