"""ASR 服务目录定义。"""

from app.domain.service_catalog import ServiceCatalogItem

ASR_SERVICE = ServiceCatalogItem(
    code="asr-v1",
    name="ASR 服务",
    models=(),
    quota_unit="seconds",
)
