import hashlib
import json
import logging
import re
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
RESPONSE_BLOCKLIST = HOP_BY_HOP_HEADERS | {"content-length", "content-encoding", "set-cookie"}
OAUTH_ENDPOINT_METADATA_FIELDS = {
    "authorize": "authorization_endpoint",
    "token": "token_endpoint",
    "register": "registration_endpoint",
}
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
TOOL_NAME_MAX_LENGTH = 50
_TOOL_NAME_REWRITES: dict[tuple[str, str, str], dict[str, str]] = {}
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
) -> StreamingResponse:
    litellm_key = await repository.get_litellm_key(user_sub)
    if not litellm_key:
        raise HTTPException(
            status_code=412,
            detail=f"Vault incomplete. Open {settings.public_url}/ and add your LiteLLM key.",
        )

    secrets = await repository.get_secrets(user_sub, mcp_name)
    body = await request.body()
    tool_name_scope = _tool_name_scope("litellm", mcp_name, user_sub)
    upstream_body = _rewrite_tool_call_request_body(body, tool_name_scope)
    upstream_headers = _upstream_headers(request.headers, mcp_name, litellm_key, secrets)
    url = _upstream_url(settings, mcp_name, subpath, request.url.query)
    upstream_request = http_client.build_request(
        request.method,
        url,
        headers=upstream_headers,
        content=upstream_body,
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    _log_litellm_mcp_request(
        mcp_name=mcp_name,
        body=upstream_body,
        status_code=upstream_response.status_code,
        saved_secret_headers=secrets.keys(),
        injected_secret_headers=_litellm_mcp_secret_headers(mcp_name, secrets).keys(),
    )

    return await _mcp_response(
        upstream_response,
        request_body=body,
        tool_name_scope=tool_name_scope,
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
        tool_name_scope=_tool_name_scope(
            "delegated-litellm",
            mcp_name,
            _authorization_scope(request.headers),
        ),
        response_headers_rewrite_mcp_name=mcp_name,
    )


async def proxy_delegated_litellm_request(
    *,
    request: Request,
    path: str,
    settings: Settings,
    http_client: httpx.AsyncClient,
    tool_name_scope: tuple[str, str, str] | None = None,
    preserve_response_cookies: bool = False,
    response_headers_rewrite_mcp_name: str | None = None,
) -> StreamingResponse:
    url = httpx.URL(f"{settings.litellm_base_url}{path}").copy_with(query=request.url.query.encode("utf-8"))
    body = await request.body()
    upstream_body = _rewrite_tool_call_request_body(body, tool_name_scope) if tool_name_scope else body
    upstream_request = http_client.build_request(
        request.method,
        url,
        headers=_delegated_upstream_headers(request.headers),
        content=upstream_body,
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    if tool_name_scope is not None:
        return await _mcp_response(
            upstream_response,
            request_body=body,
            tool_name_scope=tool_name_scope,
            response_headers=(
                _litellm_response_headers(
                    upstream_response.headers,
                    settings,
                    response_headers_rewrite_mcp_name,
                )
                if response_headers_rewrite_mcp_name is not None
                else None
            ),
        )
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers, preserve_cookies=preserve_response_cookies),
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
    body = await request.body()
    tool_name_scope = _tool_name_scope("direct-broker", server.name, user_sub)
    upstream_body = _rewrite_tool_call_request_body(body, tool_name_scope)
    url_param_secret_names = set(server.url_param_secrets)
    url_params = {name: value for name, value in secrets.items() if name in url_param_secret_names}
    header_secrets = {name: value for name, value in secrets.items() if name not in url_param_secret_names}
    upstream_request = http_client.build_request(
        request.method,
        _direct_mcp_url(server.direct_url, subpath, request.url.query, url_params or None),
        headers=_direct_broker_upstream_headers(request.headers, header_secrets, server.static_headers),
        content=upstream_body,
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return await _mcp_response(
        upstream_response,
        request_body=body,
        tool_name_scope=tool_name_scope,
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
    body = await request.body()
    tool_name_scope = _tool_name_scope(
        "direct-passthrough",
        server.name,
        _authorization_scope(request.headers),
    )
    upstream_body = _rewrite_tool_call_request_body(body, tool_name_scope)
    upstream_request = http_client.build_request(
        request.method,
        _direct_mcp_url(server.direct_url, subpath, request.url.query),
        headers=_direct_upstream_headers(request.headers, server),
        content=upstream_body,
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return await _mcp_response(
        upstream_response,
        request_body=body,
        tool_name_scope=tool_name_scope,
        response_headers=_direct_response_headers(upstream_response.headers, settings, server),
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
    headers = _direct_upstream_headers(request.headers, server)
    upstream_url = await _direct_oauth_endpoint_url(
        server=server,
        endpoint=endpoint,
        query=_rewrite_direct_oauth_resource_params(request.url.query, settings, server),
        headers=headers,
        http_client=http_client,
    )
    upstream_request = http_client.build_request(
        request.method,
        upstream_url,
        headers=_direct_oauth_headers(headers, upstream_url),
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

    last_response: httpx.Response | None = None
    headers = _direct_upstream_headers(request.headers, server)
    for upstream_url in _direct_metadata_urls(server.direct_url, metadata_kind, request.url.query):
        try:
            upstream_response = await http_client.get(upstream_url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "Direct OAuth metadata request failed mcp=%s kind=%s url=%s error=%s",
                server.name,
                metadata_kind,
                upstream_url,
                exc,
            )
            continue

        last_response = upstream_response
        try:
            payload = upstream_response.json()
        except ValueError:
            _log_direct_oauth_metadata_response_failure(
                server=server,
                metadata_kind=metadata_kind,
                upstream_url=upstream_url,
                upstream_response=upstream_response,
                reason="invalid_json",
            )
            continue
        if upstream_response.status_code >= 400:
            _log_direct_oauth_metadata_response_failure(
                server=server,
                metadata_kind=metadata_kind,
                upstream_url=upstream_url,
                upstream_response=upstream_response,
                reason="status_error",
            )
            continue
        return JSONResponse(
            _rewrite_direct_metadata(payload, settings, server),
            status_code=upstream_response.status_code,
            headers=_response_headers(upstream_response.headers),
        )

    if last_response is None:
        raise HTTPException(status_code=502, detail="Direct OAuth metadata upstream request failed")

    try:
        payload = last_response.json()
    except ValueError:
        return JSONResponse(
            {"detail": "Direct OAuth metadata upstream returned non-JSON response"},
            status_code=502,
        )
    return JSONResponse(
        _rewrite_direct_metadata(payload, settings, server),
        status_code=last_response.status_code,
        headers=_response_headers(last_response.headers),
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
    static_headers: Mapping[str, str],
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming.items()
        if name.lower() not in REQUEST_BLOCKLIST | {"x-litellm-api-key"}
    }
    headers.update(secrets)
    headers.update(static_headers)
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
    # Direct OAuth upstreams may validate Origin against their own origin.
    headers = {
        name: value
        for name, value in headers.items()
        if name.lower() not in {"origin", "referer"}
    }
    headers["origin"] = upstream_origin
    return headers


def _direct_oauth_headers(headers: dict[str, str], upstream_url: httpx.URL) -> dict[str, str]:
    return {
        **{
            name: value
            for name, value in headers.items()
            if name.lower() != "origin"
        },
        "origin": _url_origin(upstream_url),
    }


def _direct_mcp_url(
    direct_url: str,
    subpath: str,
    query: str,
    extra_params: dict[str, str] | None = None,
) -> httpx.URL:
    url = httpx.URL(direct_url)
    base_path = url.path.rstrip("/")
    if subpath:
        clean_subpath = subpath.strip("/")
        path = f"{base_path}/{clean_subpath}" if base_path else f"/{clean_subpath}"
    else:
        path = base_path or "/"
    if extra_params:
        merged = parse_qsl(query, keep_blank_values=True) + list(extra_params.items())
        query = urlencode(merged)
    return url.copy_with(path=path, query=query.encode("utf-8"))


def _direct_oauth_url(direct_url: str, endpoint: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    parent = url.path.rstrip("/").rsplit("/", 1)[0]
    path = f"{parent}/{endpoint}" if parent else f"/{endpoint}"
    return url.copy_with(path=path, query=query.encode("utf-8"))


async def _direct_oauth_endpoint_url(
    *,
    server: McpServerConfiguration,
    endpoint: str,
    query: str,
    headers: dict[str, str],
    http_client: httpx.AsyncClient,
) -> httpx.URL:
    metadata_field = OAUTH_ENDPOINT_METADATA_FIELDS.get(endpoint)
    if metadata_field:
        metadata_url = await _direct_oauth_metadata_endpoint_url(
            server=server,
            metadata_field=metadata_field,
            headers=headers,
            http_client=http_client,
        )
        if metadata_url is not None:
            return metadata_url.copy_with(query=query.encode("utf-8"))

    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    return _direct_oauth_url(server.direct_url, endpoint, query)


async def _direct_oauth_metadata_endpoint_url(
    *,
    server: McpServerConfiguration,
    metadata_field: str,
    headers: dict[str, str],
    http_client: httpx.AsyncClient,
) -> httpx.URL | None:
    if not server.direct_url:
        return None

    for upstream_url in _direct_metadata_urls(server.direct_url, "oauth-authorization-server", ""):
        try:
            upstream_response = await http_client.get(upstream_url, headers=headers)
        except httpx.HTTPError:
            continue
        if upstream_response.status_code >= 400:
            continue
        try:
            payload = upstream_response.json()
        except ValueError:
            continue
        endpoint_url = _metadata_endpoint_url(payload, metadata_field)
        if endpoint_url is not None:
            return endpoint_url
    return None


def _metadata_endpoint_url(payload: object, metadata_field: str) -> httpx.URL | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(metadata_field)
    if not isinstance(value, str) or not value.strip():
        return None
    url = httpx.URL(value.strip())
    if url.scheme not in {"http", "https"} or not url.host:
        return None
    return url


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


def _direct_metadata_urls(direct_url: str, metadata_kind: str, query: str) -> list[httpx.URL]:
    candidates = [_direct_metadata_url(direct_url, metadata_kind, query)]
    if metadata_kind == "oauth-authorization-server":
        url = httpx.URL(direct_url)
        resource_path = url.path.strip("/")
        if resource_path:
            candidates.append(
                url.copy_with(
                    path=f"/.well-known/oauth-authorization-server/{resource_path}",
                    query=query.encode("utf-8"),
                )
            )
        candidates.append(url.copy_with(path="/.well-known/openid-configuration", query=query.encode("utf-8")))

    deduped: list[httpx.URL] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _response_headers(incoming: Mapping[str, str], *, preserve_cookies: bool = False) -> dict[str, str]:
    blocklist = RESPONSE_BLOCKLIST - {"set-cookie"} if preserve_cookies else RESPONSE_BLOCKLIST
    return {
        name: value
        for name, value in incoming.items()
        if name.lower() not in blocklist
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


def _litellm_response_headers(
    incoming: Mapping[str, str],
    settings: Settings,
    mcp_name: str,
) -> dict[str, str]:
    return {
        name: _rewrite_litellm_metadata_string(value, settings, mcp_name)
        for name, value in _response_headers(incoming).items()
    }


async def _mcp_response(
    response: httpx.Response,
    *,
    request_body: bytes,
    tool_name_scope: tuple[str, str, str],
    response_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    rewritten_body = await _tools_list_response_body(response, request_body, tool_name_scope)
    return StreamingResponse(
        _single_response_body(rewritten_body) if rewritten_body is not None else _response_body(response),
        status_code=response.status_code,
        headers=response_headers if response_headers is not None else _response_headers(response.headers),
        background=BackgroundTask(response.aclose),
    )


async def _tools_list_response_body(
    response: httpx.Response,
    request_body: bytes,
    tool_name_scope: tuple[str, str, str],
) -> bytes | None:
    request_payload = _json_payload(request_body)
    if not _contains_jsonrpc_method(request_payload, "tools/list"):
        return None

    body = await response.aread()
    _TOOL_NAME_REWRITES[tool_name_scope] = {}
    response_payload = _json_payload(body)
    if response_payload is None:
        sse_rewrite = _rewrite_tools_list_sse_body(body)
        if sse_rewrite is not None:
            rewritten_body, rewritten_to_original = sse_rewrite
            _TOOL_NAME_REWRITES[tool_name_scope] = rewritten_to_original
            return rewritten_body
        return body

    rewritten_payload, rewritten_to_original = _rewrite_tools_list_payload(response_payload)
    _TOOL_NAME_REWRITES[tool_name_scope] = rewritten_to_original
    if rewritten_payload == response_payload:
        return body
    return json.dumps(rewritten_payload, separators=(",", ":")).encode("utf-8")


def _rewrite_tools_list_sse_body(body: bytes) -> tuple[bytes, dict[str, str]] | None:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None

    parsed_data = False
    rewritten_to_original: dict[str, str] = {}
    rewritten_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        rewritten_line, line_mapping, parsed = _rewrite_sse_data_line(line)
        parsed_data = parsed_data or parsed
        rewritten_to_original.update(line_mapping)
        rewritten_lines.append(rewritten_line)

    if not parsed_data:
        return None
    return "".join(rewritten_lines).encode("utf-8"), rewritten_to_original


def _rewrite_sse_data_line(line: str) -> tuple[str, dict[str, str], bool]:
    content, newline = _split_line_ending(line)
    if not content.startswith("data:"):
        return line, {}, False

    data = content[5:]
    separator = ""
    if data.startswith(" "):
        separator = " "
        data = data[1:]

    payload = _json_payload(data.encode("utf-8"))
    if payload is None:
        return line, {}, False

    rewritten_payload, rewritten_to_original = _rewrite_tools_list_payload(payload)
    if rewritten_payload == payload:
        return line, rewritten_to_original, True

    rewritten_data = json.dumps(rewritten_payload, separators=(",", ":"))
    return f"data:{separator}{rewritten_data}{newline}", rewritten_to_original, True


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


async def _single_response_body(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def _response_body(response: httpx.Response) -> AsyncIterator[bytes]:
    if hasattr(response, "_content"):
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


def _rewrite_tool_call_request_body(body: bytes, tool_name_scope: tuple[str, str, str]) -> bytes:
    rewritten_to_original = _TOOL_NAME_REWRITES.get(tool_name_scope)
    if not rewritten_to_original:
        return body
    payload = _json_payload(body)
    if payload is None:
        return body
    rewritten_payload = _rewrite_tool_call_payload(payload, rewritten_to_original)
    if rewritten_payload == payload:
        return body
    return json.dumps(rewritten_payload, separators=(",", ":")).encode("utf-8")


def _rewrite_tool_call_payload(value: object, rewritten_to_original: Mapping[str, str]) -> object:
    if isinstance(value, list):
        return [_rewrite_tool_call_payload(item, rewritten_to_original) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten = {
        key: _rewrite_tool_call_payload(item, rewritten_to_original)
        for key, item in value.items()
    }
    if rewritten.get("method") != "tools/call":
        return rewritten
    params = rewritten.get("params")
    if not isinstance(params, dict):
        return rewritten
    name = params.get("name")
    if not isinstance(name, str) or name not in rewritten_to_original:
        return rewritten
    rewritten["params"] = {**params, "name": rewritten_to_original[name]}
    return rewritten


def _rewrite_tools_list_payload(value: object) -> tuple[object, dict[str, str]]:
    used_names: set[str] = set()
    rewritten_to_original: dict[str, str] = {}

    def rewrite(child: object) -> object:
        if isinstance(child, list):
            return [rewrite(item) for item in child]
        if not isinstance(child, dict):
            return child

        rewritten: dict[object, object] = {}
        for key, item in child.items():
            if key == "tools" and isinstance(item, list):
                rewritten[key] = [_rewrite_tool(item) for item in item]
            else:
                rewritten[key] = rewrite(item)
        return rewritten

    def _rewrite_tool(tool: object) -> object:
        if not isinstance(tool, dict):
            return tool
        name = tool.get("name")
        if not isinstance(name, str):
            return tool

        rewritten_name = _unique_tool_name(name, used_names)
        used_names.add(rewritten_name)
        if rewritten_name == name:
            return tool

        rewritten_to_original[rewritten_name] = name
        return {**tool, "name": rewritten_name}

    return rewrite(value), rewritten_to_original


def _unique_tool_name(name: str, used_names: set[str]) -> str:
    candidate = _safe_tool_name(name)
    if candidate not in used_names:
        return candidate

    counter = 2
    while True:
        suffix = f"_{counter}"
        stem = candidate[: TOOL_NAME_MAX_LENGTH - len(suffix)].rstrip("_-") or "tool"
        unique_candidate = f"{stem}{suffix}"
        if unique_candidate not in used_names:
            return unique_candidate
        counter += 1


def _safe_tool_name(name: str) -> str:
    if TOOL_NAME_RE.fullmatch(name):
        return name

    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    normalized = re.sub(r"_+", "_", normalized) or "tool"
    return normalized[:TOOL_NAME_MAX_LENGTH].rstrip("_-") or "tool"


def _json_payload(body: bytes) -> object | None:
    if not body:
        return None
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return None


def _contains_jsonrpc_method(value: object, method: str) -> bool:
    if isinstance(value, dict):
        return value.get("method") == method
    if isinstance(value, list):
        return any(_contains_jsonrpc_method(item, method) for item in value)
    return False


def _tool_name_scope(kind: str, server_name: str, owner: str) -> tuple[str, str, str]:
    return kind, server_name, owner


def _authorization_scope(headers: Mapping[str, str]) -> str:
    value = headers.get("authorization") or ""
    if not value:
        return "anonymous"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rewrite_litellm_metadata(value: object, settings: Settings, mcp_name: str) -> object:
    if isinstance(value, str):
        return _rewrite_litellm_metadata_string(value, settings, mcp_name)
    if isinstance(value, list):
        return [_rewrite_litellm_metadata(item, settings, mcp_name) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_litellm_metadata(item, settings, mcp_name) for key, item in value.items()}
    return value


def _rewrite_litellm_metadata_string(value: str, settings: Settings, mcp_name: str) -> str:
    rewritten = value.replace(settings.litellm_base_url, settings.public_url)
    rewritten = rewritten.replace(f"/.well-known/oauth-protected-resource/{mcp_name}/mcp", f"/.well-known/oauth-protected-resource/{mcp_name}")
    rewritten = rewritten.replace(f"/.well-known/oauth-authorization-server/{mcp_name}/mcp", f"/.well-known/oauth-authorization-server/{mcp_name}")
    rewritten = rewritten.replace(f"/{mcp_name}/mcp", f"/{mcp_name}")
    return rewritten


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


def _log_direct_oauth_metadata_response_failure(
    *,
    server: McpServerConfiguration,
    metadata_kind: str,
    upstream_url: httpx.URL,
    upstream_response: httpx.Response,
    reason: str,
) -> None:
    logger.warning(
        "Direct OAuth metadata request failed mcp=%s kind=%s url=%s status=%s reason=%s body=%r",
        server.name,
        metadata_kind,
        upstream_url,
        upstream_response.status_code,
        reason,
        _response_text_excerpt(upstream_response),
    )


def _response_text_excerpt(response: httpx.Response, limit: int = 500) -> str:
    text = response.text
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


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
