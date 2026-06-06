import json

import httpx
import pytest

from mcp_broker.mcp_health import McpToolsHealthClient
from mcp_broker.storage import McpServerConfiguration
from tests.conftest import FakeRepository

pytestmark = pytest.mark.anyio


async def test_mcp_tools_health_lists_litellm_tools_with_saved_headers(settings) -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode("utf-8"))
        captured.append(
            {
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}},
                headers={"mcp-session-id": "session-1"},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "dokploy.deploy"}]},
                },
            )
        return httpx.Response(400)

    repository = FakeRepository(
        secrets={"dokploy": {"X-DOKPLOY_API_KEY": "dokploy-key"}}
    )
    server = McpServerConfiguration(
        name="dokploy",
        required_headers=("X-DOKPLOY_API_KEY",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        statuses = await McpToolsHealthClient(settings, http_client).check_servers(
            user_sub="pocket-sub",
            repository=repository,
            servers=[server],
        )

    assert statuses["dokploy"].state == "healthy"
    assert statuses["dokploy"].label == "Tools healthy"
    assert statuses["dokploy"].detail == "1 tool listed by tools/list."
    assert [request["body"]["method"] for request in captured] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert captured[0]["path"] == "/dokploy/mcp"
    assert captured[0]["headers"]["accept"] == "application/json, text/event-stream"
    assert captured[0]["headers"]["x-litellm-api-key"] == "Bearer litellm-user-key"
    assert captured[0]["headers"]["x-dokploy_api_key"] == "dokploy-key"
    assert captured[0]["headers"]["x-mcp-dokploy-x-dokploy_api_key"] == "dokploy-key"
    assert captured[2]["headers"]["mcp-session-id"] == "session-1"


async def test_mcp_tools_health_marks_empty_tools_as_unhealthy(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode("utf-8"))
        if body["method"] == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}},
        )

    server = McpServerConfiguration(name="dokploy", required_headers=())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        statuses = await McpToolsHealthClient(settings, http_client).check_servers(
            user_sub="pocket-sub",
            repository=FakeRepository(),
            servers=[server],
        )

    assert statuses["dokploy"].state == "unhealthy"
    assert statuses["dokploy"].label == "No tools"
    assert statuses["dokploy"].detail == "tools/list returned zero tools."


async def test_mcp_tools_health_does_not_probe_oauth_passthrough_servers(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("passthrough servers should not be probed from the dashboard")

    server = McpServerConfiguration(
        name="googlemcp",
        required_headers=(),
        delegated_auth_passthrough=True,
        auth_type="oauth2",
        source="direct",
        direct_url="https://googlemcp.example.com/mcp",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        statuses = await McpToolsHealthClient(settings, http_client).check_servers(
            user_sub="pocket-sub",
            repository=FakeRepository(),
            servers=[server],
        )

    assert statuses["googlemcp"].state == "unknown"
    assert statuses["googlemcp"].label == "Health unknown"
    assert "client OAuth session" in statuses["googlemcp"].detail
