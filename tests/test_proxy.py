import json
import logging
import re

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


async def test_proxy_rewrites_invalid_tool_names_and_restores_original_on_call(settings) -> None:
    invalid_tool_name = "google.drive:search documents with a tool name that is much too long"
    captured_bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        captured_bodies.append(payload)
        if payload["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {"name": invalid_tool_name, "description": "Search files"},
                            {"name": "already_valid_1", "description": "Already valid"},
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
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
        list_response = await client.post(
            "/dokploy",
            headers={"Authorization": "Bearer oauth-access-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        rewritten_name = list_response.json()["result"]["tools"][0]["name"]
        call_response = await client.post(
            "/dokploy",
            headers={"Authorization": "Bearer oauth-access-token"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": rewritten_name, "arguments": {"query": "budget"}},
            },
        )

    assert list_response.status_code == 200
    assert call_response.status_code == 200
    assert rewritten_name != invalid_tool_name
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,50}", rewritten_name)
    assert list_response.json()["result"]["tools"][1]["name"] == "already_valid_1"
    tool_calls = [body for body in captured_bodies if body.get("method") == "tools/call"]
    assert tool_calls[0]["params"]["name"] == invalid_tool_name


async def test_proxy_rewrites_invalid_tool_names_in_sse_tools_list(settings) -> None:
    invalid_tool_name = "autobrowser-browser.get_auth_profile"
    captured_bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(await request.aread())
        captured_bodies.append(payload)
        if payload["method"] == "tools/list":
            message = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"tools": [{"name": invalid_tool_name, "description": "Navigate"}]},
            }
            return httpx.Response(
                200,
                content=f"event: message\ndata: {json.dumps(message)}\n\n".encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
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
        list_response = await client.post(
            "/autobrowser",
            headers={"Authorization": "Bearer oauth-access-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        data_line = next(line for line in list_response.text.splitlines() if line.startswith("data: "))
        rewritten_name = json.loads(data_line.removeprefix("data: "))["result"]["tools"][0]["name"]
        call_response = await client.post(
            "/autobrowser",
            headers={"Authorization": "Bearer oauth-access-token"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": rewritten_name, "arguments": {}},
            },
        )

    assert list_response.status_code == 200
    assert call_response.status_code == 200
    assert rewritten_name == "autobrowser-browser_get_auth_profile"
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,50}", rewritten_name)
    tool_calls = [body for body in captured_bodies if body.get("method") == "tools/call"]
    assert tool_calls[0]["params"]["name"] == invalid_tool_name


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
                "custommcp": {
                    "Authorization": "Bearer upstream-custom-token",
                    "X-Custom-MCP-API-Key": "custom-secret",
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
            "/custommcp",
            headers={"Authorization": "Bearer oauth-access-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
        )

    assert response.status_code == 401
    assert "mcp=custommcp" in caplog.text
    assert "method=tools/call" in caplog.text
    assert "upstream_status=401" in caplog.text
    assert "Authorization" in caplog.text
    assert "X-Custom-MCP-API-Key" in caplog.text
    assert "upstream-custom-token" not in caplog.text
    assert "custom-secret" not in caplog.text
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


async def test_direct_broker_auth_mcp_proxies_static_headers_and_suffix(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(200, content=b"ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            litellm_key=None,
            secrets={"hindsight": {"Authorization": "Bearer user-secret-that-must-not-win"}},
            mcp_servers={
                "hindsight": McpServerConfiguration(
                    name="hindsight",
                    required_headers=(),
                    delegated_auth_passthrough=False,
                    auth_type=None,
                    source="direct",
                    direct_url="https://api.hindsight.example.com/mcp",
                    static_headers={"Authorization": "Basic upstream-secret"},
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
            "/hindsight/tartanpion",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b"{}",
        )

    assert response.status_code == 200
    assert captured["path"] == "/mcp/tartanpion"
    assert captured["query"] == ""
    assert captured["body"] == b"{}"
    assert captured["headers"]["authorization"] == "Basic upstream-secret"
    assert "x-litellm-api-key" not in captured["headers"]


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


async def test_delegated_auth_mcp_rewrites_oauth_challenge_metadata(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "invalid_token"},
            headers={
                "www-authenticate": (
                    'Bearer error="invalid_token", '
                    'resource_metadata="https://litellm.example.com/.well-known/oauth-protected-resource/github/mcp"'
                )
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
        response = await client.post("/github", content=b"{}")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer error="invalid_token", '
        'resource_metadata="https://broker.example.com/.well-known/oauth-protected-resource/github"'
    )


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
            return httpx.Response(
                302,
                headers={
                    "location": "https://oauth.example.com/authorize",
                    "set-cookie": "litellm_oauth_state=state; Path=/; HttpOnly; SameSite=Lax",
                },
            )
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
    assert authorize_response.headers["set-cookie"].startswith("litellm_oauth_state=state")
    assert token_response.status_code == 200
    assert captured[0][0] == "GET"
    assert captured[0][1] == "/github/authorize"
    assert captured[1][0] == "POST"
    assert captured[1][1] == "/github/token"
    assert captured[1][2] == b"grant_type=authorization_code&code=abc"
    assert "x-litellm-api-key" not in captured[0][3]
    assert "x-litellm-api-key" not in captured[1][3]


async def test_delegated_auth_callback_proxies_to_litellm_with_oauth_cookie(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = request.url.query.decode()
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            302,
            headers={
                "location": "https://openwebui.example.com/oauth/callback?code=client-code",
                "set-cookie": "litellm_oauth_state=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            },
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/callback?code=upstream-code&state=opaque-state",
            headers={"Cookie": "litellm_oauth_state=state"},
        )

    assert response.status_code == 302
    assert response.headers["location"] == "https://openwebui.example.com/oauth/callback?code=client-code"
    assert response.headers["set-cookie"].startswith("litellm_oauth_state=;")
    assert captured["path"] == "/callback"
    assert captured["query"] == "code=upstream-code&state=opaque-state"
    assert captured["headers"]["cookie"] == "litellm_oauth_state=state"
    assert "x-litellm-api-key" not in captured["headers"]


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
        if request.url.path.startswith("/.well-known/"):
            return httpx.Response(404, content=b"missing")
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
    assert [item[1] for item in captured] == [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/.well-known/openid-configuration",
        "/authorize",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/.well-known/openid-configuration",
        "/token",
    ]
    assert captured[3][0] == "GET"
    assert captured[3][2] == (
        "client_id=standard-mcp-client&resource=https%3A%2F%2Fgooglemcp.example.com%2Fmcp"
    )
    assert captured[7][0] == "POST"
    assert captured[7][3] == (
        b"grant_type=authorization_code&code=abc"
        b"&resource=https%3A%2F%2Fgooglemcp.example.com%2Fmcp"
    )
    assert captured[3][4]["origin"] == "https://googlemcp.example.com"
    assert captured[7][4]["origin"] == "https://googlemcp.example.com"
    assert "referer" not in captured[3][4]
    assert "referer" not in captured[7][4]
    assert "x-litellm-api-key" not in captured[3][4]
    assert "x-litellm-api-key" not in captured[7][4]


async def test_direct_passthrough_oauth_endpoints_use_advertised_metadata_urls(settings) -> None:
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
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(404, content=b"missing")
        if request.url.path == "/.well-known/oauth-authorization-server/mcp":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://generic.example.com/oauth2",
                    "authorization_endpoint": "https://generic.example.com/oauth2/authorize",
                    "token_endpoint": "https://generic.example.com/oauth2/token",
                    "registration_endpoint": "https://generic.example.com/oauth2/register",
                },
            )
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"location": "https://auth.example.com/continue"})
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "upstream-token"})
        if request.url.path == "/oauth2/register":
            return httpx.Response(201, json={"client_id": "registered-client"})
        return httpx.Response(500, content=f"unexpected {request.url.path}".encode())

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "generic": McpServerConfiguration(
                    name="generic",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://generic.example.com/mcp",
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
            "/generic/authorize?client_id=standard-mcp-client"
            "&resource=https%3A%2F%2Fbroker.example.com%2Fgeneric",
            headers={"Origin": "https://broker.example.com"},
        )
        token_response = await client.post(
            "/generic/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://broker.example.com",
            },
            content=(
                b"grant_type=authorization_code&code=abc"
                b"&resource=https%3A%2F%2Fbroker.example.com%2Fgeneric"
            ),
        )
        register_response = await client.post(
            "/generic/register",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://broker.example.com",
            },
            content=b'{"redirect_uris":["https://client.example.com/callback"]}',
        )

    assert authorize_response.status_code == 302
    assert authorize_response.headers["location"] == "https://auth.example.com/continue"
    assert token_response.status_code == 200
    assert register_response.status_code == 201
    assert [item[1] for item in captured] == [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/oauth2/authorize",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/oauth2/token",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
        "/oauth2/register",
    ]
    assert captured[2][2] == (
        "client_id=standard-mcp-client&resource=https%3A%2F%2Fgeneric.example.com%2Fmcp"
    )
    assert captured[5][3] == (
        b"grant_type=authorization_code&code=abc"
        b"&resource=https%3A%2F%2Fgeneric.example.com%2Fmcp"
    )
    assert captured[8][3] == b'{"redirect_uris":["https://client.example.com/callback"]}'
    assert captured[2][4]["origin"] == "https://generic.example.com"
    assert captured[5][4]["origin"] == "https://generic.example.com"
    assert captured[8][4]["origin"] == "https://generic.example.com"
