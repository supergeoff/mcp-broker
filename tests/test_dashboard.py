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


async def test_dashboard_uses_dokploy_shell_and_system_theme(settings, fake_repository) -> None:
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
    assert 'class="app-shell"' in response.text
    assert "@media (prefers-color-scheme: dark)" in response.text
    assert "Dashboard" in response.text
    assert "MCP discovery" in response.text
    assert "admin@example.com" in response.text


async def test_dashboard_renders_light_dark_system_theme_toggle(settings, fake_repository) -> None:
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
    assert 'class="theme-toggle"' in response.text
    assert 'data-theme-choice="light"' in response.text
    assert 'data-theme-choice="dark"' in response.text
    assert 'data-theme-choice="system"' in response.text
    assert "mcp-broker-theme" in response.text
    assert 'html[data-theme="light"]' in response.text
    assert 'html[data-theme="dark"]' in response.text


async def test_admin_uses_dokploy_shell_and_status_badges(settings, fake_repository) -> None:
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
        response = await client.get("/admin")

    assert response.status_code == 200
    assert 'class="app-shell"' in response.text
    assert 'class="data-table"' in response.text
    assert 'class="status-badge status-saved"' in response.text
    assert "admin@example.com" in response.text


async def test_discovery_partial_uses_result_cards_for_named_mcp(settings, fake_repository) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("x-litellm-api-key")
        if request.url.path == "/v1/mcp/server" and auth == "Bearer admin-read-key":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"name": "dokploy", "env": {"TOKEN": "${X-DOKPLOY-TOKEN}"}},
                    ]
                },
            )
        if request.url.path == "/v1/mcp/server" and auth == "Bearer litellm-user-key":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"name": "dokploy", "env": {"TOKEN": "${X-DOKPLOY-TOKEN}"}},
                    ]
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as litellm_client:
        app = create_app(settings=settings, repository=fake_repository, http_client=litellm_client)
        cookie = _session_cookie(
            settings.session_secret,
            {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            client.cookies.set("session", cookie)
            response = await client.post("/api/discover")

    assert response.status_code == 200
    assert 'class="server-card"' in response.text
    assert 'name="mcp_name" value="dokploy"' in response.text
    assert "X-DOKPLOY-TOKEN" in response.text
    assert "Saved" in response.text
