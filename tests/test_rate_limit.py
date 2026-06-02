import httpx
import pytest

from mcp_broker.app import create_app
from tests.conftest import FakeJwtValidator, FakeRepository

pytestmark = pytest.mark.anyio


async def test_mcp_proxy_rate_limits_per_user(settings) -> None:
    limited_settings = settings.model_copy(update={"rate_limit_requests_per_minute": 1})

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    app = create_app(
        settings=limited_settings,
        repository=FakeRepository(),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        first = await client.post("/dokploy", headers={"Authorization": "Bearer oauth-access-token"})
        second = await client.post("/dokploy", headers={"Authorization": "Bearer oauth-access-token"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {"detail": "Too many MCP requests. Try again later."}
