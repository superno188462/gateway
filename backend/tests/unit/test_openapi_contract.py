"""提交的 OpenAPI 契约与实际应用保持一致。"""

from pathlib import Path
from typing import Any

import yaml

from app.bootstrap import create_app
from app.config import Settings

CONTRACT_PATH = Path(__file__).parents[3] / "contracts" / "openapi.yaml"


class PassingProbe:
    """避免合同测试访问真实数据库。"""

    async def check(self) -> None:
        return None


TEST_SETTINGS = Settings(
    database_url="postgresql+asyncpg://user:pass@localhost/db", jwt_secret_key="x" * 32
)


def test_committed_openapi_paths_match_generated_openapi() -> None:
    committed: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    generated = create_app(settings=TEST_SETTINGS, readiness_probe=PassingProbe()).openapi()

    assert set(committed["paths"]) == set(generated["paths"])
    assert committed["info"]["title"] == generated["info"]["title"]

    for path, operations in committed["paths"].items():
        methods = {key for key in operations if key != "parameters"}
        assert methods == set(generated["paths"][path])
