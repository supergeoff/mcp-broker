import gzip
import json
import logging

import httpx
import pytest

from mcp_broker.app import create_app
from mcp_broker.storage import McpServerConfiguration
from tests.conftest import FakeRepository

pytestmark = pytest.mark.anyio


async def test_root_protected_resource_metadata_advertises_pocket_id_scopes(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert response.json()["scopes_supported"] == ["openid", "email", "profile"]


async def test_named_protected_resource_metadata_points_clients_to_pocket_id(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource/dokploy")

    assert response.status_code == 200
    assert response.json() == {
        "resource": "https://broker.example.com/dokploy",
        "authorization_servers": ["https://id.example.com"],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "email", "profile"],
        "resource_documentation": "https://broker.example.com/",
    }


async def test_named_subpath_protected_resource_metadata_points_clients_to_pocket_id(
    settings,
    fake_repository,
) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource/hindsight/geoff")

    assert response.status_code == 200
    assert response.json() == {
        "resource": "https://broker.example.com/hindsight/geoff",
        "authorization_servers": ["https://id.example.com"],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "email", "profile"],
        "resource_documentation": "https://broker.example.com/",
    }


async def test_direct_passthrough_protected_resource_metadata_is_proxied_and_rewritten(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "resource": "https://googlemcp.example.com/mcp",
                "authorization_servers": [
                    "https://googlemcp.example.com/"
                ],
            },
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/.well-known/oauth-protected-resource/googlemcp",
            headers={
                "Origin": "https://broker.example.com",
                "Referer": "https://broker.example.com/googlemcp",
            },
        )

    assert response.status_code == 200
    assert captured["path"] == "/.well-known/oauth-protected-resource/mcp"
    assert captured["headers"]["origin"] == "https://googlemcp.example.com"
    assert "referer" not in captured["headers"]
    assert response.json() == {
        "resource": "https://broker.example.com/googlemcp",
        "authorization_servers": [
            "https://broker.example.com/googlemcp"
        ],
    }


async def test_direct_passthrough_authorization_server_metadata_is_proxied_and_rewritten(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "issuer": "https://googlemcp.example.com",
                "authorization_endpoint": "https://googlemcp.example.com/authorize",
                "token_endpoint": "https://googlemcp.example.com/token",
                "registration_endpoint": "https://googlemcp.example.com/register",
            },
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-authorization-server/googlemcp")

    assert response.status_code == 200
    assert captured["path"] == "/.well-known/oauth-authorization-server"
    assert response.json() == {
        "issuer": "https://broker.example.com/googlemcp",
        "authorization_endpoint": "https://broker.example.com/googlemcp/authorize",
        "token_endpoint": "https://broker.example.com/googlemcp/token",
        "registration_endpoint": "https://broker.example.com/googlemcp/register",
    }


async def test_direct_passthrough_metadata_drops_upstream_content_encoding(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "issuer": "https://googlemcp.example.com",
            "authorization_endpoint": "https://googlemcp.example.com/authorize",
            "token_endpoint": "https://googlemcp.example.com/token",
            "registration_endpoint": "https://googlemcp.example.com/register",
        }
        return httpx.Response(
            200,
            content=gzip.compress(json.dumps(payload).encode("utf-8")),
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-authorization-server/googlemcp")

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.json()["issuer"] == "https://broker.example.com/googlemcp"


async def test_direct_passthrough_authorization_server_metadata_falls_back_to_path_variant(
    settings,
    caplog,
) -> None:
    captured_paths: list[str] = []
    caplog.set_level(logging.WARNING, logger="mcp_broker.proxy")

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.path)
        if request.url.path == "/.well-known/oauth-authorization-server/mcp":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://googlemcp.example.com",
                    "authorization_endpoint": "https://googlemcp.example.com/authorize",
                    "token_endpoint": "https://googlemcp.example.com/token",
                    "registration_endpoint": "https://googlemcp.example.com/register",
                },
            )
        return httpx.Response(404, content=b"missing")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-authorization-server/googlemcp")

    assert response.status_code == 200
    assert captured_paths == [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
    ]
    assert response.json()["issuer"] == "https://broker.example.com/googlemcp"
    assert "Direct OAuth metadata request failed" in caplog.text


async def test_named_mcp_without_bearer_token_returns_oauth_challenge(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/dokploy", content=b"{}")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://broker.example.com/.well-known/oauth-protected-resource/dokploy"'
    )


async def test_named_mcp_subpath_without_bearer_token_returns_subpath_oauth_challenge(
    settings,
    fake_repository,
) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/hindsight/geoff", content=b"{}")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://broker.example.com/.well-known/oauth-protected-resource/hindsight/geoff"'
    )


async def test_legacy_mcp_path_is_reserved(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/mcp", content=b"{}")

    assert response.status_code == 404
