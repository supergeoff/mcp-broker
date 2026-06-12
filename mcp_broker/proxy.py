import json
import logging
from hashlib import sha256
from collections.abc import Mapping
from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode

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
DIRECT_LITELLM_SECRET_BLOCKLIST = (REQUEST_BLOCKLIST - {"authorization"}) | {"x-litellm-api-key"}
RESPONSE_BLOCKLIST = HOP_BY_HOP_HEADERS | {"content-length", "set-cookie"}
logger = logging.getLogger(__name__)


async def proxy_mcp_request(
    *,
    request: Request,
    mcp_name: str,
    subpath: str,
    user_sub: str,
    settings: Settings,
    repository: Repository,
    http_client: httpx.AsyncClient,
    litellm_mcp_name: str | None = None,
    secrets_override: Mapping[str, str] | None = None,
) -> StreamingResponse:
    litellm_key = await repository.get_litellm_key(user_sub)
    if not litellm_key:
        raise HTTPException(
            status_code=412,
            detail=f"Vault incomplete. Open {settings.public_url}/ and add your LiteLLM key.",
        )

    secrets = dict(secrets_override) if secrets_override is not None else await repository.get_secrets(user_sub, mcp_name)
    body = await request.body()
    upstream_headers = _upstream_headers(request.headers, mcp_name, litellm_key, secrets)
    url = _upstream_url(settings, litellm_mcp_name or mcp_name, subpath, request.url.query)
    upstream_request = http_client.build_request(
        request.method,
        url,
        headers=upstream_headers,
        content=body,
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    _log_litellm_mcp_request(
        mcp_name=mcp_name,
        body=body,
        status_code=upstream_response.status_code,
        saved_secret_headers=secrets.keys(),
        injected_secret_headers=_litellm_mcp_secret_headers(mcp_name, secrets).keys(),
    )

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
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    upstream_request = http_client.build_request(
        request.method,
        _direct_mcp_url(server.direct_url, subpath, request.url.query),
        headers=_direct_upstream_headers(request.headers, server),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_direct_response_headers(upstream_response.headers, settings, server),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_direct_oauth_endpoint_request(
    *,
    request: Request,
    server: McpServerConfiguration,
    endpoint: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    body = await request.body()
    upstream_request = http_client.build_request(
        request.method,
        _direct_oauth_url(
            server.direct_url,
            endpoint,
            _rewrite_direct_oauth_resource_params(request.url.query, settings, server),
        ),
        headers=_direct_upstream_headers(request.headers, server),
        content=_rewrite_direct_oauth_form_body(
            body,
            request.headers.get("content-type"),
            settings,
            server,
        ),
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
    upstream_response = await http_client.get(upstream_url, headers=_direct_upstream_headers(request.headers, server))
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


async def ensure_hindsight_bank_litellm_server(
    *,
    bank_id: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
) -> str:
    server_name = _hindsight_bank_litellm_server_name(bank_id)
    servers_response = await http_client.get(
        f"{settings.litellm_base_url}/v1/mcp/server",
        headers={"x-litellm-api-key": litellm_auth_value(settings.litellm_admin_key)},
    )
    servers_response.raise_for_status()
    servers = servers_response.json()
    if not isinstance(servers, list):
        raise HTTPException(status_code=502, detail="LiteLLM MCP catalog response is invalid")

    for server in servers:
        if isinstance(server, dict) and server.get("server_name") == server_name:
            return server_name

    base_server = next(
        (
            server
            for server in servers
            if isinstance(server, dict) and server.get("server_name") == "hindsight"
        ),
        None,
    )
    if not isinstance(base_server, dict):
        raise HTTPException(status_code=502, detail="LiteLLM Hindsight server is not configured")

    base_url = str(base_server.get("url") or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=502, detail="LiteLLM Hindsight server is missing url")

    create_response = await http_client.post(
        f"{settings.litellm_base_url}/v1/mcp/server",
        headers={
            "x-litellm-api-key": litellm_auth_value(settings.litellm_admin_key),
            "litellm-changed-by": "mcp-broker",
        },
        json={
            "server_name": server_name,
            "alias": f"hindsight/{bank_id}",
            "description": f"Hindsight memory bank {bank_id}",
            "url": f"{base_url}/{bank_id}",
            "transport": base_server.get("transport") or "http",
            "auth_type": base_server.get("auth_type") or "none",
            "static_headers": base_server.get("static_headers"),
            "mcp_access_groups": base_server.get("mcp_access_groups") or ["All"],
            "available_on_public_internet": bool(base_server.get("available_on_public_internet", True)),
        },
    )
    create_response.raise_for_status()
    return server_name


def _hindsight_bank_litellm_server_name(bank_id: str) -> str:
    digest = sha256(bank_id.encode("utf-8")).hexdigest()[:16]
    return f"hindsight-bank-{digest}"


def _upstream_url(settings: Settings, mcp_name: str, subpath: str, query: str) -> httpx.URL:
    path = f"/{mcp_name}/mcp" if not subpath else f"/{mcp_name}/mcp/{subpath}"
    return httpx.URL(f"{settings.litellm_base_url}{path}").copy_with(query=query.encode("utf-8"))


def _upstream_headers(
    incoming: Mapping[str, str],
    mcp_name: str,
    litellm_key: str,
    secrets: Mapping[str, str],
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming.items()
        if name.lower() not in REQUEST_BLOCKLIST
    }
    headers["x-litellm-api-key"] = litellm_auth_value(litellm_key)
    headers.update(_litellm_mcp_secret_headers(mcp_name, secrets))
    return headers


def _litellm_mcp_secret_headers(mcp_name: str, secrets: Mapping[str, str]) -> dict[str, str]:
    headers = {
        header_name: value
        for header_name, value in secrets.items()
        if header_name.lower() not in DIRECT_LITELLM_SECRET_BLOCKLIST
    }
    headers.update({
        f"x-mcp-{mcp_name}-{header_name.lower()}": value
        for header_name, value in secrets.items()
    })
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


def _direct_upstream_headers(
    incoming: Mapping[str, str],
    server: McpServerConfiguration,
) -> dict[str, str]:
    headers = _delegated_upstream_headers(incoming)
    if not server.direct_url:
        return headers

    upstream_origin = _url_origin(httpx.URL(server.direct_url))
    # Direct FastMCP OAuth upstreams validate Origin against their own origin.
    headers = {
        name: value
        for name, value in headers.items()
        if name.lower() not in {"origin", "referer"}
    }
    headers["origin"] = upstream_origin
    return headers


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


def _rewrite_direct_oauth_form_body(
    body: bytes,
    content_type: str | None,
    settings: Settings,
    server: McpServerConfiguration,
) -> bytes:
    if not body or not _is_form_urlencoded(content_type):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    rewritten = _rewrite_direct_oauth_resource_params(text, settings, server)
    return rewritten.encode("utf-8") if rewritten != text else body


def _is_form_urlencoded(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded"


def _rewrite_direct_oauth_resource_params(
    encoded: str,
    settings: Settings,
    server: McpServerConfiguration,
) -> str:
    if not encoded or not server.direct_url:
        return encoded

    public_resource = f"{settings.public_url}/{server.name}".rstrip("/")
    upstream_resource = server.direct_url.rstrip("/")
    changed = False
    rewritten: list[tuple[str, str]] = []
    for name, value in parse_qsl(encoded, keep_blank_values=True):
        if name == "resource" and value.rstrip("/") == public_resource:
            rewritten.append((name, upstream_resource))
            changed = True
        else:
            rewritten.append((name, value))

    return urlencode(rewritten) if changed else encoded


def _direct_metadata_url(direct_url: str, metadata_kind: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    path = f"/.well-known/{metadata_kind}"
    if metadata_kind == "oauth-protected-resource":
        resource_path = url.path.strip("/")
        if resource_path:
            path = f"{path}/{resource_path}"
    return url.copy_with(path=path, query=query.encode("utf-8"))


def _response_headers(incoming: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in incoming.items()
        if name.lower() not in RESPONSE_BLOCKLIST
    }


def _direct_response_headers(
    incoming: Mapping[str, str],
    settings: Settings,
    server: McpServerConfiguration,
) -> dict[str, str]:
    return {
        name: _rewrite_direct_metadata_string(value, settings, server)
        for name, value in _response_headers(incoming).items()
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
    upstream = httpx.URL(server.direct_url)
    direct_url = str(upstream).rstrip("/")
    upstream_origin = _url_origin(upstream)
    rewritten = value.replace(direct_url, public_mcp_url)

    for metadata_kind in ("oauth-protected-resource", "oauth-authorization-server"):
        upstream_metadata = str(_direct_metadata_url(server.direct_url, metadata_kind, "")).rstrip("?")
        public = f"{settings.public_url}/.well-known/{metadata_kind}/{server.name}"
        rewritten = rewritten.replace(upstream_metadata, public)

    for endpoint in ("authorize", "token", "register"):
        upstream_endpoint = str(_direct_oauth_url(server.direct_url, endpoint, "")).rstrip("?")
        rewritten = rewritten.replace(upstream_endpoint, f"{public_mcp_url}/{endpoint}")

    if rewritten in {upstream_origin, f"{upstream_origin}/"}:
        return public_mcp_url
    rewritten = rewritten.replace(f"{upstream_origin}/", f"{public_mcp_url}/")
    rewritten = rewritten.replace(upstream_origin, public_mcp_url)
    return rewritten


def _url_origin(url: httpx.URL) -> str:
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{url.host}{port}"


def _log_litellm_mcp_request(
    *,
    mcp_name: str,
    body: bytes,
    status_code: int,
    saved_secret_headers: object,
    injected_secret_headers: object,
) -> None:
    method, tool_name = _mcp_request_info(body)
    log = logger.warning if status_code >= 400 else logger.info
    log(
        "MCP upstream request mcp=%s method=%s tool=%s upstream=litellm upstream_status=%s "
        "saved_secret_headers=%s injected_secret_headers=%s",
        mcp_name,
        method,
        tool_name,
        status_code,
        _safe_header_names(saved_secret_headers),
        _safe_header_names(injected_secret_headers),
    )


def _mcp_request_info(body: bytes) -> tuple[str, str]:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return "-", "-"
    return _jsonrpc_method(payload), _jsonrpc_tool_name(payload)


def _jsonrpc_method(payload: object) -> str:
    if isinstance(payload, dict):
        method = payload.get("method")
        return str(method)[:80] if method else "-"
    if isinstance(payload, list):
        methods = [_jsonrpc_method(item) for item in payload]
        methods = [method for method in methods if method != "-"]
        return ",".join(methods[:3]) if methods else "-"
    return "-"


def _jsonrpc_tool_name(payload: object) -> str:
    if isinstance(payload, dict):
        if payload.get("method") != "tools/call":
            return "-"
        params = payload.get("params")
        if not isinstance(params, dict):
            return "-"
        name = params.get("name")
        return str(name)[:120] if name else "-"
    if isinstance(payload, list):
        names = [_jsonrpc_tool_name(item) for item in payload]
        names = [name for name in names if name != "-"]
        return ",".join(names[:3]) if names else "-"
    return "-"


def _safe_header_names(header_names: object) -> tuple[str, ...]:
    if not isinstance(header_names, Mapping) and not hasattr(header_names, "__iter__"):
        return ()
    return tuple(sorted(str(name) for name in header_names))
