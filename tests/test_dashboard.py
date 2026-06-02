import json
from base64 import b64encode

import httpx
import pytest
from itsdangerous import TimestampSigner

from mcp_broker.app import create_app

pytestmark = pytest.mark.anyio


def _session_cookie(secret: str, payload: dict[str, object]) -> str:
    data = b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(secret).sign(data).decode("utf-8")


async def test_dashboard_renders_after_successful_login(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert "admin@example.com" in response.text


async def test_dashboard_saves_secret_header_for_named_mcp(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/secret",
            data={
                "mcp_name": "dokploy",
                "header_name": "X-DOKPLOY_API_KEY",
                "value": "dokploy-key",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.secrets["dokploy"]["X-DOKPLOY_API_KEY"] == "dokploy-key"
