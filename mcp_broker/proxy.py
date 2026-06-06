from collections.abc import Mapping
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from mcp_broker.config import Settings
from mcp_broker.security import litellm_auth_value
from mcp_broker.storage import McpServerConfiguration, Repository

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_BLOCKLIST = HOP_BY_HOP_HEADERS | {"authorization", "content-length", "host"}
DELEGATED_REQUEST_BLOCKLIST = HOP_BY_HOP_HEADERS | {"content-length", "host", "x-litellm-api-key"}
RESPONSE_BLOCKLIST = HOP_BY_HOP_HEADERS | {"content-length", "set-cookie"}


async def proxy_mcp_request(
    *,
    request: Request,
    mcp_name: str,
    subpath: str,
    user_sub: str,
    settings: Settings,
    repository: Repository,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    litellm_key = await repository.get_litellm_key(user_sub)
    if not litellm_key:
        raise HTTPException(
            status_code=412,
            detail=f"Vault incomplete. Open {settings.public_url}/ and add your LiteLLM key.",
        )

    secrets = await repository.get_secrets(user_sub, mcp_name)
    url = _upstream_url(settings, mcp_name, subpath, request.url.query)
    upstream_request = http_client.build_request(
        request.method,
        url,
        headers=_upstream_headers(request.headers, litellm_key, secrets),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)

    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_delegated_mcp_request(
    *,
    request: Request,
    mcp_name: str,
    subpath: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    path = f"/{mcp_name}/mcp" if not subpath else f"/{mcp_name}/mcp/{subpath}"
    return await proxy_delegated_litellm_request(
        request=request,
        path=path,
        settings=settings,
        http_client=http_client,
    )


async def proxy_delegated_litellm_request(
    *,
    request: Request,
    path: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    url = httpx.URL(f"{settings.litellm_base_url}{path}").copy_with(query=request.url.query.encode("utf-8"))
    upstream_request = http_client.build_request(
        request.method,
        url,
        headers=_delegated_upstream_headers(request.headers),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_direct_broker_mcp_request(
    *,
    request: Request,
    server: McpServerConfiguration,
    subpath: str,
    user_sub: str,
    repository: Repository,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    secrets = await repository.get_secrets(user_sub, server.name)
    upstream_request = http_client.build_request(
        request.method,
        _direct_mcp_url(server.direct_url, subpath, request.url.query),
        headers=_direct_broker_upstream_headers(request.headers, secrets),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_direct_passthrough_mcp_request(
    *,
    request: Request,
    server: McpServerConfiguration,
    subpath: str,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    upstream_request = http_client.build_request(
        request.method,
        _direct_mcp_url(server.direct_url, subpath, request.url.query),
        headers=_delegated_upstream_headers(request.headers),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_direct_oauth_endpoint_request(
    *,
    request: Request,
    server: McpServerConfiguration,
    endpoint: str,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    upstream_request = http_client.build_request(
        request.method,
        _direct_oauth_url(server.direct_url, endpoint, request.url.query),
        headers=_delegated_upstream_headers(request.headers),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_direct_oauth_metadata_request(
    *,
    request: Request,
    server: McpServerConfiguration,
    metadata_kind: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> JSONResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    upstream_url = _direct_metadata_url(server.direct_url, metadata_kind, request.url.query)
    upstream_response = await http_client.get(upstream_url, headers=_delegated_upstream_headers(request.headers))
    try:
        payload = upstream_response.json()
    except ValueError:
        payload = {}
    return JSONResponse(
        _rewrite_direct_metadata(payload, settings, server),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
    )


async def proxy_delegated_oauth_metadata_request(
    *,
    request: Request,
    mcp_name: str,
    path: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> JSONResponse:
    url = httpx.URL(f"{settings.litellm_base_url}{path}").copy_with(query=request.url.query.encode("utf-8"))
    upstream_response = await http_client.get(url, headers=_delegated_upstream_headers(request.headers))
    try:
        payload = upstream_response.json()
    except ValueError:
        payload = {}
    return JSONResponse(
        _rewrite_litellm_metadata(payload, settings, mcp_name),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
    )


def _upstream_url(settings: Settings, mcp_name: str, subpath: str, query: str) -> httpx.URL:
    path = f"/{mcp_name}/mcp" if not subpath else f"/{mcp_name}/mcp/{subpath}"
    return httpx.URL(f"{settings.litellm_base_url}{path}").copy_with(query=query.encode("utf-8"))


def _upstream_headers(
    incoming: Mapping[str, str],
    litellm_key: str,
    secrets: Mapping[str, str],
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming.items()
        if name.lower() not in REQUEST_BLOCKLIST
    }
    headers["x-litellm-api-key"] = litellm_auth_value(litellm_key)
    headers.update(secrets)
    return headers


def _direct_broker_upstream_headers(
    incoming: Mapping[str, str],
    secrets: Mapping[str, str],
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming.items()
        if name.lower() not in REQUEST_BLOCKLIST | {"x-litellm-api-key"}
    }
    headers.update(secrets)
    return headers


def _delegated_upstream_headers(incoming: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in incoming.items()
        if name.lower() not in DELEGATED_REQUEST_BLOCKLIST
    }


def _direct_mcp_url(direct_url: str, subpath: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    base_path = url.path.rstrip("/")
    if subpath:
        clean_subpath = subpath.strip("/")
        path = f"{base_path}/{clean_subpath}" if base_path else f"/{clean_subpath}"
    else:
        path = base_path or "/"
    return url.copy_with(path=path, query=query.encode("utf-8"))


def _direct_oauth_url(direct_url: str, endpoint: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    parent = url.path.rstrip("/").rsplit("/", 1)[0]
    path = f"{parent}/{endpoint}" if parent else f"/{endpoint}"
    return url.copy_with(path=path, query=query.encode("utf-8"))


def _direct_metadata_url(direct_url: str, metadata_kind: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    resource_path = url.path.strip("/")
    path = f"/.well-known/{metadata_kind}"
    if resource_path:
        path = f"{path}/{resource_path}"
    return url.copy_with(path=path, query=query.encode("utf-8"))


def _response_headers(incoming: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in incoming.items()
        if name.lower() not in RESPONSE_BLOCKLIST
    }


async def _response_body(response: httpx.Response) -> AsyncIterator[bytes]:
    if hasattr(response, "_content"):
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


def _rewrite_litellm_metadata(value: object, settings: Settings, mcp_name: str) -> object:
    if isinstance(value, str):
        rewritten = value.replace(settings.litellm_base_url, settings.public_url)
        rewritten = rewritten.replace(f"/.well-known/oauth-protected-resource/{mcp_name}/mcp", f"/.well-known/oauth-protected-resource/{mcp_name}")
        rewritten = rewritten.replace(f"/.well-known/oauth-authorization-server/{mcp_name}/mcp", f"/.well-known/oauth-authorization-server/{mcp_name}")
        rewritten = rewritten.replace(f"/{mcp_name}/mcp", f"/{mcp_name}")
        return rewritten
    if isinstance(value, list):
        return [_rewrite_litellm_metadata(item, settings, mcp_name) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_litellm_metadata(item, settings, mcp_name) for key, item in value.items()}
    return value


def _rewrite_direct_metadata(value: object, settings: Settings, server: McpServerConfiguration) -> object:
    if isinstance(value, str):
        return _rewrite_direct_metadata_string(value, settings, server)
    if isinstance(value, list):
        return [_rewrite_direct_metadata(item, settings, server) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_direct_metadata(item, settings, server) for key, item in value.items()}
    return value


def _rewrite_direct_metadata_string(value: str, settings: Settings, server: McpServerConfiguration) -> str:
    if not server.direct_url:
        return value
    public_mcp_url = f"{settings.public_url}/{server.name}"
    direct_url = str(httpx.URL(server.direct_url)).rstrip("/")
    rewritten = value.replace(direct_url, public_mcp_url)

    for metadata_kind in ("oauth-protected-resource", "oauth-authorization-server"):
        upstream = str(_direct_metadata_url(server.direct_url, metadata_kind, "")).rstrip("?")
        public = f"{settings.public_url}/.well-known/{metadata_kind}/{server.name}"
        rewritten = rewritten.replace(upstream, public)

    for endpoint in ("authorize", "token", "register"):
        upstream = str(_direct_oauth_url(server.direct_url, endpoint, "")).rstrip("?")
        rewritten = rewritten.replace(upstream, f"{public_mcp_url}/{endpoint}")
    return rewritten
