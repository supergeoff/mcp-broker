import logging
from hashlib import sha256

import httpx
import pytest

from mcp_broker.app import create_app
from mcp_broker.storage import McpServerConfiguration
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
            "/dokploy?stream=true",
            headers={"Authorization": "Bearer oauth-access-token", "Connection": "close"},
            content=b'{"jsonrpc":"2.0"}',
        )

    assert response.status_code == 200
    assert response.text == "event: ready\n\n"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "set-cookie" not in response.headers
    assert captured["path"] == "/dokploy/mcp"
    assert captured["query"] == "stream=true"
    assert captured["body"] == b'{"jsonrpc":"2.0"}'
    assert captured["headers"]["x-litellm-api-key"] == "Bearer litellm-user-key"
    assert captured["headers"]["x-dokploy-token"] == "dokploy-user-token"
    assert captured["headers"]["x-mcp-dokploy-x-dokploy-token"] == "dokploy-user-token"
    assert "authorization" not in captured["headers"]


async def test_named_mcp_route_targets_litellm_server_mcp_and_scopes_headers(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            secrets={
                "dokploy": {
                    "X-DOKPLOY_URL": "https://dokploy.example.com",
                    "X-DOKPLOY_API_KEY": "dokploy-key",
                },
                "context7": {"X-CONTEXT7-API-KEY": "context7-key"},
            }
        ),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/dokploy",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b"{}",
        )

    assert response.status_code == 200
    assert captured["path"] == "/dokploy/mcp"
    assert captured["headers"]["x-dokploy_url"] == "https://dokploy.example.com"
    assert captured["headers"]["x-dokploy_api_key"] == "dokploy-key"
    assert captured["headers"]["x-mcp-dokploy-x-dokploy_url"] == "https://dokploy.example.com"
    assert captured["headers"]["x-mcp-dokploy-x-dokploy_api_key"] == "dokploy-key"
    assert "x-context7-api-key" not in captured["headers"]
    assert "x-mcp-context7-x-context7-api-key" not in captured["headers"]


async def test_litellm_proxy_forwards_saved_authorization_without_leaking_oauth_authorization(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            secrets={
                "gworkspace": {
                    "Authorization": "Bearer upstream-google-token",
                    "X-API-Key": "workspace-api-key",
                }
            }
        ),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/gworkspace",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b"{}",
        )

    assert response.status_code == 200
    assert captured["headers"]["x-litellm-api-key"] == "Bearer litellm-user-key"
    assert captured["headers"]["authorization"] == "Bearer upstream-google-token"
    assert captured["headers"]["x-mcp-gworkspace-authorization"] == "Bearer upstream-google-token"
    assert captured["headers"]["x-mcp-gworkspace-x-api-key"] == "workspace-api-key"
    assert captured["headers"]["x-api-key"] == "workspace-api-key"


async def test_litellm_proxy_logs_secret_header_names_without_values_on_auth_failure(settings, caplog) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            secrets={
                "openwebui": {
                    "Authorization": "Bearer upstream-openwebui-token",
                    "X-OpenWebUI-API-Key": "openwebui-secret",
                }
            }
        ),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    caplog.set_level(logging.INFO, logger="mcp_broker.proxy")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/openwebui",
            headers={"Authorization": "Bearer oauth-access-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
        )

    assert response.status_code == 401
    assert "mcp=openwebui" in caplog.text
    assert "method=tools/call" in caplog.text
    assert "upstream_status=401" in caplog.text
    assert "Authorization" in caplog.text
    assert "X-OpenWebUI-API-Key" in caplog.text
    assert "upstream-openwebui-token" not in caplog.text
    assert "openwebui-secret" not in caplog.text
    assert "oauth-access-token" not in caplog.text


async def test_named_mcp_subpath_routes_under_litellm_server_mcp(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        return httpx.Response(200, content=b"ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(secrets={"context7": {"X-CONTEXT7-API-KEY": "context7-key"}}),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/context7/events?cursor=abc",
            headers={"Authorization": "Bearer oauth-access-token"},
        )

    assert response.status_code == 200
    assert captured["path"] == "/context7/mcp/events"
    assert captured["query"] == "cursor=abc"


async def test_hindsight_bank_path_routes_through_litellm_bank_server(settings) -> None:
    captured: dict[str, object] = {}
    bank_server_name = f"hindsight-bank-{sha256('tartanpion'.encode('utf-8')).hexdigest()[:16]}"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/mcp/server":
            return httpx.Response(
                200,
                json=[
                    {
                        "server_id": "base-hindsight-id",
                        "server_name": "hindsight",
                        "url": "https://api.hindsight.example.com/mcp",
                        "transport": "http",
                        "auth_type": "none",
                        "static_headers": {"Authorization": "Basic upstream-secret"},
                        "mcp_access_groups": ["All"],
                        "available_on_public_internet": True,
                    }
                ],
            )
        if request.method == "POST" and request.url.path == "/v1/mcp/server":
            captured["created_server"] = await request.aread()
            return httpx.Response(
                201,
                json={
                    "server_id": "bank-hindsight-id",
                    "server_name": bank_server_name,
                    "url": "https://api.hindsight.example.com/mcp/tartanpion",
                },
            )
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, content=b"ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(secrets={"hindsight": {"X-Bank-Id": "wrong-bank"}}),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/hindsight/tartanpion",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b"{}",
        )

    assert response.status_code == 200
    assert captured["path"] == f"/{bank_server_name}/mcp"
    assert captured["headers"]["x-bank-id"] == "tartanpion"
    assert captured["headers"]["x-mcp-hindsight-x-bank-id"] == "tartanpion"
    assert b'"server_name":"' + bank_server_name.encode("utf-8") + b'"' in captured["created_server"]
    assert b'"url":"https://api.hindsight.example.com/mcp/tartanpion"' in captured["created_server"]
    assert b'"Authorization":"Basic upstream-secret"' in captured["created_server"]


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
            "/dokploy",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b"{}",
        )

    assert response.status_code == 412
    assert response.json() == {
        "detail": "Vault incomplete. Open https://broker.example.com/ and add your LiteLLM key."
    }


async def test_direct_broker_auth_mcp_proxies_without_litellm_key(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(200, content=b"direct-ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            litellm_key=None,
            secrets={
                "googlemcp": {
                    "Authorization": "Bearer direct-upstream-token",
                    "X-GOOGLE-WORKSPACE": "workspace-token",
                    "X-Bank-Id": "geoff",
                }
            },
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=("Authorization", "X-GOOGLE-WORKSPACE", "X-Bank-Id"),
                    delegated_auth_passthrough=False,
                    auth_type=None,
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            },
        ),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/googlemcp?stream=true",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b'{"jsonrpc":"2.0"}',
        )

    assert response.status_code == 200
    assert response.text == "direct-ok"
    assert captured["url"] == "https://googlemcp.example.com/mcp?stream=true"
    assert captured["body"] == b'{"jsonrpc":"2.0"}'
    assert captured["headers"]["authorization"] == "Bearer direct-upstream-token"
    assert captured["headers"]["x-google-workspace"] == "workspace-token"
    assert captured["headers"]["x-bank-id"] == "geoff"
    assert "x-litellm-api-key" not in captured["headers"]


async def test_direct_passthrough_mcp_preserves_authorization_without_pocket_id(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(200, content=b"direct-passthrough-ok")

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
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/googlemcp/events?cursor=abc",
            headers={
                "Authorization": "Bearer upstream-token",
                "Origin": "https://broker.example.com",
                "Referer": "https://broker.example.com/googlemcp",
            },
            content=b"{}",
        )

    assert response.status_code == 200
    assert response.text == "direct-passthrough-ok"
    assert captured["url"] == "https://googlemcp.example.com/mcp/events?cursor=abc"
    assert captured["body"] == b"{}"
    assert captured["headers"]["authorization"] == "Bearer upstream-token"
    assert captured["headers"]["origin"] == "https://googlemcp.example.com"
    assert "referer" not in captured["headers"]
    assert "x-litellm-api-key" not in captured["headers"]


async def test_direct_passthrough_mcp_rewrites_oauth_challenge_metadata(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "invalid_token"},
            headers={
                "www-authenticate": (
                    'Bearer error="invalid_token", '
                    'resource_metadata="https://googlemcp.example.com/.well-known/oauth-protected-resource/mcp"'
                )
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
        response = await client.post("/googlemcp", content=b"{}")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer error="invalid_token", '
        'resource_metadata="https://broker.example.com/.well-known/oauth-protected-resource/googlemcp"'
    )


async def test_delegated_auth_mcp_proxies_without_pocket_id_and_preserves_authorization(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(200, content=b"delegated-ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "github": McpServerConfiguration(
                    name="github",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                )
            }
        ),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/github",
            headers={"Authorization": "Bearer upstream-oauth-token"},
            content=b'{"jsonrpc":"2.0"}',
        )

    assert response.status_code == 200
    assert response.text == "delegated-ok"
    assert captured["path"] == "/github/mcp"
    assert captured["body"] == b'{"jsonrpc":"2.0"}'
    assert captured["headers"]["authorization"] == "Bearer upstream-oauth-token"
    assert "x-litellm-api-key" not in captured["headers"]


async def test_delegated_auth_metadata_is_proxied_to_litellm_legacy_mcp_oauth_endpoint(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "resource": "https://litellm.example.com/github/mcp",
                "authorization_servers": [
                    "https://litellm.example.com/.well-known/oauth-authorization-server/github/mcp"
                ],
            },
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "github": McpServerConfiguration(
                    name="github",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource/github")

    assert response.status_code == 200
    assert captured["path"] == "/.well-known/oauth-protected-resource/github/mcp"
    assert response.json() == {
        "resource": "https://broker.example.com/github",
        "authorization_servers": [
            "https://broker.example.com/.well-known/oauth-authorization-server/github"
        ],
    }


async def test_delegated_auth_oauth_endpoints_proxy_to_litellm_without_litellm_key(settings) -> None:
    captured: list[tuple[str, str, bytes, dict[str, str]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, request.url.path, await request.aread(), dict(request.headers)))
        if request.url.path == "/github/authorize":
            return httpx.Response(302, headers={"location": "https://oauth.example.com/authorize"})
        return httpx.Response(200, json={"access_token": "token"})

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "github": McpServerConfiguration(
                    name="github",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        authorize_response = await client.get("/github/authorize?client_id=standard-mcp-client")
        token_response = await client.post(
            "/github/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            content=b"grant_type=authorization_code&code=abc",
        )

    assert authorize_response.status_code == 302
    assert authorize_response.headers["location"] == "https://oauth.example.com/authorize"
    assert token_response.status_code == 200
    assert captured[0][0] == "GET"
    assert captured[0][1] == "/github/authorize"
    assert captured[1][0] == "POST"
    assert captured[1][1] == "/github/token"
    assert captured[1][2] == b"grant_type=authorization_code&code=abc"
    assert "x-litellm-api-key" not in captured[0][3]
    assert "x-litellm-api-key" not in captured[1][3]


async def test_direct_passthrough_oauth_endpoints_map_to_upstream_siblings(settings) -> None:
    captured: list[tuple[str, str, str, bytes, dict[str, str]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.method,
                request.url.path,
                request.url.query.decode(),
                await request.aread(),
                dict(request.headers),
            )
        )
        if request.url.path == "/authorize":
            return httpx.Response(302, headers={"location": "https://accounts.google.com/o/oauth2/v2/auth"})
        return httpx.Response(200, json={"access_token": "upstream-token"})

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
        follow_redirects=False,
    ) as client:
        authorize_response = await client.get(
            "/googlemcp/authorize?client_id=standard-mcp-client"
            "&resource=https%3A%2F%2Fbroker.example.com%2Fgooglemcp",
            headers={
                "Origin": "https://broker.example.com",
                "Referer": "https://broker.example.com/googlemcp",
            },
        )
        token_response = await client.post(
            "/googlemcp/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://broker.example.com",
                "Referer": "https://broker.example.com/googlemcp",
            },
            content=(
                b"grant_type=authorization_code&code=abc"
                b"&resource=https%3A%2F%2Fbroker.example.com%2Fgooglemcp"
            ),
        )

    assert authorize_response.status_code == 302
    assert authorize_response.headers["location"] == "https://accounts.google.com/o/oauth2/v2/auth"
    assert token_response.status_code == 200
    assert captured[0][0] == "GET"
    assert captured[0][1] == "/authorize"
    assert captured[0][2] == (
        "client_id=standard-mcp-client&resource=https%3A%2F%2Fgooglemcp.example.com%2Fmcp"
    )
    assert captured[1][0] == "POST"
    assert captured[1][1] == "/token"
    assert captured[1][3] == (
        b"grant_type=authorization_code&code=abc"
        b"&resource=https%3A%2F%2Fgooglemcp.example.com%2Fmcp"
    )
    assert captured[0][4]["origin"] == "https://googlemcp.example.com"
    assert captured[1][4]["origin"] == "https://googlemcp.example.com"
    assert "referer" not in captured[0][4]
    assert "referer" not in captured[1][4]
    assert "x-litellm-api-key" not in captured[0][4]
    assert "x-litellm-api-key" not in captured[1][4]
