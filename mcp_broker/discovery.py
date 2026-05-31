import re
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_broker.config import Settings
from mcp_broker.security import litellm_auth_value

HEADER_REF_RE = re.compile(r"\$\{(X-[^}]+)\}")


@dataclass(frozen=True)
class DiscoveredServer:
    name: str
    required_headers: tuple[str, ...]


def normalize_servers_response(payload: Any) -> list[DiscoveredServer]:
    items = _server_items(payload)
    servers: list[DiscoveredServer] = []
    for fallback_name, item in items:
        if not isinstance(item, dict):
            continue
        name = _server_name(item, fallback_name)
        if not name:
            continue
        servers.append(
            DiscoveredServer(
                name=name,
                required_headers=tuple(sorted(_extract_header_refs(item))),
            )
        )
    return sorted(servers, key=lambda server: server.name)


class DiscoveryClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http_client = http_client

    async def discover_for_user(self, user_litellm_key: str) -> list[DiscoveredServer]:
        catalog = normalize_servers_response(
            await self._get_json("/v1/mcp/server", self._settings.litellm_admin_key)
        )
        accessible_names = await self._accessible_server_names(user_litellm_key, catalog)
        return [server for server in catalog if server.name in accessible_names]

    async def _accessible_server_names(
        self,
        user_litellm_key: str,
        catalog: list[DiscoveredServer],
    ) -> set[str]:
        try:
            user_servers = normalize_servers_response(
                await self._get_json("/v1/mcp/server", user_litellm_key)
            )
        except httpx.HTTPStatusError:
            tools_payload = await self._get_json("/v1/mcp/tools", user_litellm_key)
            return _names_from_tools(tools_payload, {server.name for server in catalog})
        return {server.name for server in user_servers}

    async def _get_json(self, path: str, litellm_key: str) -> Any:
        response = await self._http_client.get(
            f"{self._settings.litellm_base_url}{path}",
            headers={"x-litellm-api-key": litellm_auth_value(litellm_key)},
        )
        response.raise_for_status()
        return response.json()


def _server_items(payload: Any) -> list[tuple[str | None, Any]]:
    if isinstance(payload, list):
        return [(None, item) for item in payload]
    if not isinstance(payload, dict):
        return []

    for key in ("servers", "data", "mcp_servers"):
        value = payload.get(key)
        if isinstance(value, list):
            return [(None, item) for item in value]
        if isinstance(value, dict):
            return [(name, item) for name, item in value.items()]

    if all(isinstance(value, dict) for value in payload.values()):
        return [(name, item) for name, item in payload.items()]
    return []


def _server_name(item: dict[str, Any], fallback_name: str | None) -> str | None:
    value = item.get("name") or item.get("server_name") or item.get("id") or fallback_name
    return str(value) if value else None


def _extract_header_refs(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(HEADER_REF_RE.findall(value))
    if isinstance(value, dict):
        found: set[str] = set()
        for child in value.values():
            found.update(_extract_header_refs(child))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for child in value:
            found.update(_extract_header_refs(child))
        return found
    return set()


def _names_from_tools(payload: Any, known_names: set[str]) -> set[str]:
    tools = payload.get("tools", payload) if isinstance(payload, dict) else payload
    if not isinstance(tools, list):
        return set()

    names: set[str] = set()
    for tool in tools:
        tool_name = tool if isinstance(tool, str) else tool.get("name") if isinstance(tool, dict) else None
        if not tool_name:
            continue
        for server_name in known_names:
            if (
                tool_name == server_name
                or tool_name.startswith(f"{server_name}.")
                or tool_name.startswith(f"{server_name}__")
                or tool_name.startswith(f"{server_name}_")
            ):
                names.add(server_name)
    return names
