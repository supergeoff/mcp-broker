import httpx
import pytest

from mcp_broker.discovery import DiscoveryClient, normalize_servers_response

pytestmark = pytest.mark.anyio


def test_normalize_servers_response_extracts_x_headers_from_litellm_shapes() -> None:
    payload = {
        "servers": [
            {
                "name": "dokploy",
                "env": {
                    "DOKPLOY_TOKEN": "${X-DOKPLOY-TOKEN}",
                    "STATIC_VALUE": "not-a-header-reference",
                },
            },
            {
                "server_name": "github",
                "config": {"env": {"GITHUB_TOKEN": "token:${X-GITHUB-TOKEN}"}},
            },
        ]
    }

    servers = normalize_servers_response(payload)

    assert [(server.name, server.required_headers) for server in servers] == [
        ("dokploy", ("X-DOKPLOY-TOKEN",)),
        ("github", ("X-GITHUB-TOKEN",)),
    ]


async def test_discovery_uses_admin_catalog_and_user_tool_fallback(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("x-litellm-api-key")
        if request.url.path == "/v1/mcp/server" and auth == "Bearer admin-read-key":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"name": "dokploy", "env": {"TOKEN": "${X-DOKPLOY-TOKEN}"}},
                        {"name": "github", "env": {"TOKEN": "${X-GITHUB-TOKEN}"}},
                    ]
                },
            )
        if request.url.path == "/v1/mcp/server" and auth == "Bearer litellm-user-key":
            return httpx.Response(403, json={"error": "forbidden"})
        if request.url.path == "/v1/mcp/tools" and auth == "Bearer litellm-user-key":
            return httpx.Response(
                200,
                json={"tools": [{"name": "dokploy.deploy"}, {"name": "dokploy__logs"}]},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        servers = await DiscoveryClient(settings, http_client).discover_for_user("litellm-user-key")

    assert [(server.name, server.required_headers) for server in servers] == [
        ("dokploy", ("X-DOKPLOY-TOKEN",))
    ]
