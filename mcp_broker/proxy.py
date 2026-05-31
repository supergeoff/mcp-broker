from collections.abc import Mapping
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request
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
RESPONSE_BLOCKLIST = HOP_BY_HOP_HEADERS | {"set-cookie"}


async def proxy_mcp_request(
    *,
    request: Request,
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

    secrets = await repository.get_secrets(user_sub)
    url = _upstream_url(settings, subpath, request.url.query)
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


def _upstream_url(settings: Settings, subpath: str, query: str) -> httpx.URL:
    path = "/mcp" if not subpath else f"/mcp/{subpath}"
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
