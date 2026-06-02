from collections.abc import Mapping
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from mcp_broker.config import Settings
from mcp_broker.security import litellm_auth_value
from mcp_broker.storage import Repository

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


def _delegated_upstream_headers(incoming: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in incoming.items()
        if name.lower() not in DELEGATED_REQUEST_BLOCKLIST
    }


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
