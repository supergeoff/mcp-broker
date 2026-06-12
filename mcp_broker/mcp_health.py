from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_broker.config import Settings
from mcp_broker.proxy import _direct_broker_upstream_headers, _litellm_mcp_secret_headers
from mcp_broker.security import litellm_auth_value
from mcp_broker.storage import MCP_SOURCE_DIRECT, Repository

MCP_HEALTH_ACCEPT = "application/json, text/event-stream"
MCP_HEALTH_TIMEOUT_SECONDS = 8.0
MCP_HEALTH_CLIENT_INFO = {"name": "mcp-broker-health", "version": "1.0"}
MCP_HEALTH_PROTOCOL_VERSION = "2025-03-26"


@dataclass(frozen=True)
class McpToolHealth:
    state: str
    label: str
    detail: str
    tool_count: int | None = None


class McpToolsHealthClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http_client = http_client

    async def check_servers(
        self,
        *,
        user_sub: str,
        repository: Repository,
        servers: Iterable[Any],
    ) -> dict[str, McpToolHealth]:
        server_list = list(servers)
        litellm_key = await repository.get_litellm_key(user_sub)
        checks = await asyncio.gather(
            *(
                self._check_server(
                    user_sub=user_sub,
                    repository=repository,
                    server=server,
                    litellm_key=litellm_key,
                )
                for server in server_list
            )
        )
        return dict(checks)

    async def _check_server(
        self,
        *,
        user_sub: str,
        repository: Repository,
        server: Any,
        litellm_key: str | None,
    ) -> tuple[str, McpToolHealth]:
        if server.delegated_auth_passthrough:
            return server.name, oauth_passthrough_health_unknown()

        source = getattr(server, "source", "litellm")
        if source == MCP_SOURCE_DIRECT:
            direct_url = getattr(server, "direct_url", None)
            if not direct_url:
                return server.name, _failed("Health check failed", "Direct MCP server is missing direct_url.")
            secrets = await repository.get_secrets(user_sub, server.name)
            return server.name, await self._probe_tools(
                httpx.URL(direct_url),
                _mcp_headers(_direct_broker_upstream_headers({}, secrets, getattr(server, "static_headers", {}))),
            )

        if not litellm_key:
            return server.name, health_unknown("Add a LiteLLM key to check tools/list.")

        secrets = await repository.get_secrets(user_sub, server.name)
        upstream_headers = {
            "x-litellm-api-key": litellm_auth_value(litellm_key),
            **_litellm_mcp_secret_headers(server.name, secrets),
        }
        return server.name, await self._probe_tools(
            httpx.URL(f"{self._settings.litellm_base_url}/{server.name}/mcp"),
            _mcp_headers(upstream_headers),
        )

    async def _probe_tools(self, url: httpx.URL, headers: dict[str, str]) -> McpToolHealth:
        try:
            initialize_response = await self._post_json(url, headers, _initialize_payload())
            if initialize_response.status_code >= 400:
                return _response_failure("initialize", initialize_response)

            session_id = initialize_response.headers.get("mcp-session-id")
            if session_id:
                headers = {**headers, "mcp-session-id": session_id}

            await self._post_json(url, headers, _initialized_payload())
            tools_response = await self._post_json(url, headers, _tools_list_payload())
        except TimeoutError:
            return _failed("Health check failed", "tools/list timed out.")
        except httpx.HTTPError as exc:
            return _failed("Health check failed", str(exc)[:500])

        if tools_response.status_code >= 400:
            return _response_failure("tools/list", tools_response)

        tools = _tools_from_response(tools_response)
        if tools is None:
            error = _jsonrpc_error_text(_response_payload(tools_response))
            detail = error or "tools/list did not return tool data."
            return _failed("Health check failed", detail)
        if not tools:
            return McpToolHealth(
                state="unhealthy",
                label="No tools",
                detail="tools/list returned zero tools.",
                tool_count=0,
            )
        return _healthy(len(tools))

    async def _post_json(self, url: httpx.URL, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
        async with asyncio.timeout(MCP_HEALTH_TIMEOUT_SECONDS):
            return await self._http_client.post(
                url,
                headers=headers,
                json=payload,
                timeout=MCP_HEALTH_TIMEOUT_SECONDS,
            )


def health_unknown(detail: str) -> McpToolHealth:
    return McpToolHealth(state="unknown", label="Health unknown", detail=detail)


def oauth_passthrough_health_unknown() -> McpToolHealth:
    return health_unknown("tools/list needs the MCP client OAuth session, so the dashboard cannot probe it directly.")


def _healthy(tool_count: int) -> McpToolHealth:
    label = "tool" if tool_count == 1 else "tools"
    return McpToolHealth(
        state="healthy",
        label="Tools healthy",
        detail=f"{tool_count} {label} listed by tools/list.",
        tool_count=tool_count,
    )


def _failed(label: str, detail: str) -> McpToolHealth:
    return McpToolHealth(state="unhealthy", label=label, detail=detail[:500])


def _response_failure(method: str, response: httpx.Response) -> McpToolHealth:
    return _failed("Health check failed", f"{method} returned {response.status_code}: {_response_snippet(response)}")


def _mcp_headers(upstream_headers: dict[str, str]) -> dict[str, str]:
    return {
        "accept": MCP_HEALTH_ACCEPT,
        "content-type": "application/json",
        **upstream_headers,
    }


def _initialize_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_HEALTH_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": MCP_HEALTH_CLIENT_INFO,
        },
    }


def _initialized_payload() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}


def _tools_list_payload() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def _tools_from_response(response: httpx.Response) -> list[Any] | None:
    return _find_tools(_response_payload(response))


def _response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return _payload_from_sse(response.text)


def _payload_from_sse(text: str) -> Any:
    for chunk in text.split("\n\n"):
        data_lines = [line.removeprefix("data:").strip() for line in chunk.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            return httpx.Response(200, content=data).json()
        except ValueError:
            continue
    return None


def _find_tools(value: Any) -> list[Any] | None:
    if isinstance(value, dict):
        tools = value.get("tools")
        if isinstance(tools, list):
            return tools
        for key in ("result", "data"):
            nested = _find_tools(value.get(key))
            if nested is not None:
                return nested
        return None
    if isinstance(value, list):
        for item in value:
            nested = _find_tools(item)
            if nested is not None:
                return nested
    return None


def _jsonrpc_error_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail")
        return str(message)[:500] if message else str(error)[:500]
    if error:
        return str(error)[:500]
    return None


def _response_snippet(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    return str(payload)[:500]
