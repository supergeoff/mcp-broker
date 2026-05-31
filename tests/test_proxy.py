import httpx
import pytest

from mcp_broker.app import create_app
from tests.conftest import FakeJwtValidator, FakeRepository

pytestmark = pytest.mark.anyio


async def test_proxy_injects_user_headers_and_removes_oauth_authorization(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(
            200,
            content=b"event: ready\n\n",
            headers={
                "content-type": "text/event-stream",
                "set-cookie": "upstream-cookie=blocked",
                "connection": "close",
            },
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/mcp/dokploy?stream=true",
            headers={"Authorization": "Bearer oauth-access-token", "Connection": "close"},
            content=b'{"jsonrpc":"2.0"}',
        )

    assert response.status_code == 200
    assert response.text == "event: ready\n\n"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "set-cookie" not in response.headers
    assert captured["path"] == "/mcp/dokploy"
    assert captured["query"] == "stream=true"
    assert captured["body"] == b'{"jsonrpc":"2.0"}'
    assert captured["headers"]["x-litellm-api-key"] == "Bearer litellm-user-key"
    assert captured["headers"]["x-dokploy-token"] == "dokploy-user-token"
    assert "authorization" not in captured["headers"]


async def test_proxy_returns_412_when_user_vault_is_not_ready(settings) -> None:
    app = create_app(
        settings=settings,
        repository=FakeRepository(litellm_key=None),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b"{}",
        )

    assert response.status_code == 412
    assert response.json() == {
        "detail": "Vault incomplete. Open https://broker.example.com/ and add your LiteLLM key."
    }
