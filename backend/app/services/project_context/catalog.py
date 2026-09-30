"""项目上下文服务对外发布的服务目录定义。"""

from app.domain.service_catalog import ServiceCatalogItem

PROJECT_CONTEXT_SERVICE = ServiceCatalogItem(
    code="project-context-v1",
    name="项目上下文服务",
    models=(),
    quota_unit=None,
)
