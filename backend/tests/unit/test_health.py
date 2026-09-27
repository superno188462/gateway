"""健康检查 HTTP 契约测试。"""

from httpx import ASGITransport, AsyncClient

from app.bootstrap import create_app
from app.config import Settings
from app.container import get_container

TEST_SETTINGS = Settings(
    database_url="postgresql+asyncpg://user:pass@localhost/db", jwt_secret_key="x" * 32
)


class PassingProbe:
    """始终就绪的测试探针。"""

    async def check(self) -> None:
        return None


class FailingProbe:
    """模拟依赖不可用且包含敏感异常文本。"""

    async def check(self) -> None:
        raise RuntimeError("password=should-never-leak")


def test_container_registers_configuration_and_probe_as_singletons() -> None:
    probe = PassingProbe()
    app = create_app(settings=TEST_SETTINGS, readiness_probe=probe)
    container = app.state.container

    assert container.settings is TEST_SETTINGS
    assert container.readiness_probe is probe
    assert get_container(type("Request", (), {"app": app})()).settings is TEST_SETTINGS


async def test_liveness_does_not_require_database() -> None:
    app = create_app(settings=TEST_SETTINGS, readiness_probe=FailingProbe())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_succeeds_when_probe_passes() -> None:
    app = create_app(settings=TEST_SETTINGS, readiness_probe=PassingProbe())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_returns_safe_error_when_probe_fails() -> None:
    app = create_app(settings=TEST_SETTINGS, readiness_probe=FailingProbe())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    payload = response.json()
    assert response.status_code == 503
    assert payload["code"] == "database_unavailable"
    assert payload["message"] == "数据库当前不可用"
    assert payload["request_id"]
    assert "password" not in response.text
