from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware

from mcp_broker.config import Settings
from mcp_broker.discovery import DiscoveryClient
from mcp_broker.litellm_health import LiteLLMHealthClient
from mcp_broker.mcp_health import McpToolHealth, McpToolsHealthClient, health_unknown
from mcp_broker.models import Base
from mcp_broker.proxy import proxy_delegated_litellm_request, proxy_delegated_mcp_request
from mcp_broker.proxy import proxy_delegated_oauth_metadata_request, proxy_mcp_request
from mcp_broker.proxy import proxy_direct_broker_mcp_request, proxy_direct_oauth_endpoint_request
from mcp_broker.proxy import proxy_direct_oauth_metadata_request, proxy_direct_passthrough_mcp_request
from mcp_broker.rate_limit import FixedWindowRateLimiter
from mcp_broker.security import FernetCipher, JwtValidationError, JwtValidator
from mcp_broker.secret_headers import is_valid_litellm_secret_header_name
from mcp_broker.secret_headers import is_valid_secret_header_name, normalize_secret_header_name
from mcp_broker.storage import MCP_SOURCE_DIRECT, McpServerConfiguration, Repository, VaultRepository

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RESERVED_MCP_NAMES = {"admin", "api", "auth", "favicon.ico", "favicon.svg", "healthz", "mcp"}
OIDC_SCOPES_SUPPORTED = ["openid", "email", "profile"]
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img">
  <title>MCP Broker</title>
  <rect width="64" height="64" rx="14" fill="#171717"/>
  <path d="M14 44V20l18 18 18-18v24" fill="none" stroke="#fafafa" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="32" y="54" fill="#fafafa" font-family="Inter, Arial, sans-serif" font-size="10" font-weight="700" text-anchor="middle">MCP</text>
</svg>"""


def create_app(
    *,
    settings: Settings | None = None,
    repository: Repository | None = None,
    jwt_validator: Any | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if repository is None:
            engine = create_async_engine(settings.database_url)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.run_sync(_ensure_mcp_server_catalog_columns)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            app.state.engine = engine
            app.state.repository = VaultRepository(
                session_factory,
                FernetCipher(settings.secrets_encryption_key),
            )

        if http_client is None:
            app.state.http_client = httpx.AsyncClient(timeout=None)

        try:
            yield
        finally:
            if http_client is None:
                client = getattr(app.state, "http_client", None)
                if client is not None:
                    await client.aclose()
            if repository is None:
                engine = getattr(app.state, "engine", None)
                if engine is not None:
                    await engine.dispose()

    app = FastAPI(title="mcp-broker", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.cookie_secure,
        same_site="lax",
    )
    app.state.settings = settings
    app.state.repository = repository
    app.state.jwt_validator = jwt_validator or JwtValidator(
        issuer=settings.issuer,
        audience=settings.expected_audience,
        jwks_uri=settings.jwks_endpoint,
    )
    app.state.rate_limiter = FixedWindowRateLimiter(limit=settings.rate_limit_requests_per_minute)
    app.state.http_client = http_client
    app.state.oauth = _oauth(settings)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> Response:
        return Response(FAVICON_SVG, media_type="image/svg+xml")

    @app.get("/.well-known/oauth-protected-resource")
    async def protected_resource_metadata() -> dict[str, object]:
        return {
            "resource": settings.public_url,
            "authorization_servers": [settings.oidc_issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": OIDC_SCOPES_SUPPORTED,
            "resource_documentation": f"{settings.public_url}/",
        }

    @app.get("/.well-known/oauth-protected-resource/{mcp_name}")
    async def named_protected_resource_metadata(request: Request, mcp_name: str):
        normalized_mcp_name = _normalize_mcp_name(mcp_name)
        server = await _repository(app).get_mcp_server(normalized_mcp_name)
        if server and server.delegated_auth_passthrough:
            if server.source == MCP_SOURCE_DIRECT:
                return await proxy_direct_oauth_metadata_request(
                    request=request,
                    server=server,
                    metadata_kind="oauth-protected-resource",
                    settings=settings,
                    http_client=_http_client(app),
                )
            return await proxy_delegated_oauth_metadata_request(
                request=request,
                mcp_name=normalized_mcp_name,
                path=f"/.well-known/oauth-protected-resource/{normalized_mcp_name}/mcp",
                settings=settings,
                http_client=_http_client(app),
            )
        return _protected_resource_metadata(settings, normalized_mcp_name)

    @app.get("/.well-known/oauth-protected-resource/{mcp_name}/{subpath:path}")
    async def named_subpath_protected_resource_metadata(request: Request, mcp_name: str, subpath: str):
        normalized_mcp_name = _normalize_mcp_name(mcp_name)
        normalized_subpath = _normalize_mcp_subpath(subpath)
        server = await _repository(app).get_mcp_server(normalized_mcp_name)
        if server and server.delegated_auth_passthrough:
            if server.source == MCP_SOURCE_DIRECT:
                return await proxy_direct_oauth_metadata_request(
                    request=request,
                    server=server,
                    metadata_kind="oauth-protected-resource",
                    settings=settings,
                    http_client=_http_client(app),
                )
            return await proxy_delegated_oauth_metadata_request(
                request=request,
                mcp_name=normalized_mcp_name,
                path=f"/.well-known/oauth-protected-resource/{normalized_mcp_name}/mcp",
                settings=settings,
                http_client=_http_client(app),
            )
        return _protected_resource_metadata(settings, normalized_mcp_name, normalized_subpath)

    @app.get("/.well-known/oauth-authorization-server/{mcp_name}")
    async def named_authorization_server_metadata(request: Request, mcp_name: str):
        normalized_mcp_name = _normalize_mcp_name(mcp_name)
        server = await _repository(app).get_mcp_server(normalized_mcp_name)
        if not server or not server.delegated_auth_passthrough:
            raise HTTPException(status_code=404, detail="MCP server not found")
        if server.source == MCP_SOURCE_DIRECT:
            return await proxy_direct_oauth_metadata_request(
                request=request,
                server=server,
                metadata_kind="oauth-authorization-server",
                settings=settings,
                http_client=_http_client(app),
            )
        return await proxy_delegated_oauth_metadata_request(
            request=request,
            mcp_name=normalized_mcp_name,
            path=f"/.well-known/oauth-authorization-server/{normalized_mcp_name}/mcp",
            settings=settings,
            http_client=_http_client(app),
        )

    def _protected_resource_metadata(settings: Settings, mcp_name: str, subpath: str = "") -> dict[str, object]:
        return {
            "resource": _public_mcp_resource(settings, mcp_name, subpath),
            "authorization_servers": [settings.oidc_issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": OIDC_SCOPES_SUPPORTED,
            "resource_documentation": f"{settings.public_url}/",
        }

    @app.get("/auth/login")
    async def auth_login(request: Request):
        redirect_uri = f"{settings.public_url}/auth/callback"
        return await app.state.oauth.pocket_id.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        token = await app.state.oauth.pocket_id.authorize_access_token(request)
        userinfo = token.get("userinfo") or await app.state.oauth.pocket_id.userinfo(token=token)
        sub = userinfo["sub"]
        email = userinfo.get("email")
        request.session["user"] = {"sub": sub, "email": email}
        await _repository(app).upsert_user(sub, email)
        return RedirectResponse("/")

    @app.get("/auth/logout")
    async def auth_logout(request: Request):
        request.session.clear()
        return RedirectResponse("/")

    @app.get("/")
    async def dashboard(request: Request):
        user = _session_user(request)
        if user is None:
            return RedirectResponse("/auth/login")
        repository = _repository(app)
        litellm_key_saved = await repository.get_litellm_key(user["sub"]) is not None
        mcp_servers = await repository.list_mcp_servers()
        active_servers = [server for server in mcp_servers if server.active]
        retired_servers = [server for server in mcp_servers if not server.active]
        catalog_names = {server.name for server in active_servers}
        secrets = {
            mcp_name: header_names
            for mcp_name, header_names in (await repository.list_secret_headers(user["sub"])).items()
            if mcp_name in catalog_names
        }
        email = str(user.get("email") or "").lower()
        is_admin = email in settings.admin_emails
        mcp_health = await _mcp_tool_health_statuses(app, settings, user["sub"], active_servers)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "request": request,
                "user": user,
                "litellm_key_saved": litellm_key_saved,
                "secrets": secrets,
                "servers": active_servers,
                "retired_servers": retired_servers,
                "mcp_health": mcp_health,
                "current_page": "dashboard",
                "is_admin": is_admin,
            },
        )

    @app.post("/api/litellm-key")
    async def save_litellm_key(request: Request):
        user = _require_session_user(request)
        form = await request.form()
        value = str(form.get("litellm_key", "")).strip()
        if not value:
            raise HTTPException(status_code=400, detail="LiteLLM key is required")
        await _repository(app).upsert_litellm_key(user["sub"], value)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/discover")
    async def discover(request: Request):
        user = _require_session_user(request)
        repository = _repository(app)
        litellm_key = await repository.get_litellm_key(user["sub"])
        if not litellm_key:
            raise HTTPException(status_code=412, detail="Add your LiteLLM key first")
        discovery = DiscoveryClient(settings, _http_client(app))
        catalog = await discovery.discover_catalog()
        await repository.upsert_mcp_servers(_mcp_server_configurations(catalog))
        servers = await discovery.discover_for_user(litellm_key, catalog)
        direct_servers = [
            server
            for server in await repository.list_mcp_servers()
            if server.source == MCP_SOURCE_DIRECT and server.active
        ]
        servers = sorted([*servers, *direct_servers], key=lambda server: server.name)
        mcp_health = await _mcp_tool_health_statuses(app, settings, user["sub"], servers)
        email = str(user.get("email") or "").lower()
        return templates.TemplateResponse(
            request=request,
            name="discover.html",
            context={
                "request": request,
                "servers": servers,
                "secrets": await repository.list_secret_headers(user["sub"]),
                "mcp_health": mcp_health,
                "is_admin": email in settings.admin_emails,
            },
        )

    @app.post("/api/secret")
    async def save_secret(request: Request):
        user = _require_session_user(request)
        form = await request.form()
        mcp_name = _normalize_mcp_name(str(form.get("mcp_name", "")).strip())
        header_name = normalize_secret_header_name(str(form.get("header_name", "")))
        value = str(form.get("value", "")).strip()
        server = await _repository(app).get_mcp_server(mcp_name)
        if not _is_valid_secret_header_for_server(header_name, server) or not value:
            raise HTTPException(status_code=400, detail="A valid header name and value are required")
        await _repository(app).upsert_secret(user["sub"], mcp_name, header_name, value)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/secret/delete")
    async def delete_secret(request: Request):
        user = _require_session_user(request)
        form = await request.form()
        mcp_name = _normalize_mcp_name(str(form.get("mcp_name", "")).strip())
        header_name = normalize_secret_header_name(str(form.get("header_name", "")))
        if not is_valid_secret_header_name(header_name):
            raise HTTPException(status_code=400, detail="A valid header name is required")
        await _repository(app).delete_secret(user["sub"], mcp_name, header_name)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/mcp/secrets/delete")
    async def delete_mcp_secrets(request: Request):
        user = _require_session_user(request)
        form = await request.form()
        mcp_name = _normalize_mcp_name(str(form.get("mcp_name", "")).strip())
        await _repository(app).delete_mcp_secrets(user["sub"], mcp_name)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/mcp/delegated-auth")
    async def save_mcp_delegated_auth(request: Request):
        user = _require_session_user(request)
        if not _is_admin_user(user, settings):
            raise HTTPException(status_code=403, detail="Admin only")
        form = await request.form()
        mcp_name = _normalize_mcp_name(str(form.get("mcp_name", "")).strip())
        enabled = str(form.get("delegated_auth_passthrough", "")).lower() in {"1", "true", "on", "yes"}
        await _repository(app).set_mcp_delegated_auth(mcp_name, enabled)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/mcp/remove")
    async def remove_retired_mcp(request: Request):
        user = _require_session_user(request)
        if not _is_admin_user(user, settings):
            raise HTTPException(status_code=403, detail="Admin only")
        form = await request.form()
        mcp_name = _normalize_mcp_name(str(form.get("mcp_name", "")).strip())
        server = next((item for item in await _repository(app).list_mcp_servers() if item.name == mcp_name), None)
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        if server.active:
            raise HTTPException(status_code=400, detail="Only retired MCP servers can be removed")
        await _repository(app).delete_mcp_server(mcp_name)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/litellm/upstream-health")
    async def check_litellm_upstream_health(request: Request):
        user = _require_session_user(request)
        if not _is_admin_user(user, settings):
            raise HTTPException(status_code=403, detail="Admin only")
        report = await LiteLLMHealthClient(settings, _http_client(app)).check_upstream_health()
        return templates.TemplateResponse(
            request=request,
            name="litellm_health.html",
            context={"request": request, "report": report},
        )

    @app.get("/admin")
    async def admin(request: Request):
        user = _require_session_user(request)
        email = str(user.get("email") or "").lower()
        if email not in settings.admin_emails:
            raise HTTPException(status_code=403, detail="Admin only")
        repository = _repository(app)
        states = await repository.list_user_states()
        direct_servers = [
            server
            for server in await repository.list_mcp_servers()
            if server.source == MCP_SOURCE_DIRECT and server.active
        ]
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={
                "request": request,
                "user": user,
                "states": states,
                "direct_servers": direct_servers,
                "current_page": "admin",
                "is_admin": True,
            },
        )

    @app.post("/api/mcp/direct")
    async def save_direct_mcp(request: Request):
        user = _require_session_user(request)
        if not _is_admin_user(user, settings):
            raise HTTPException(status_code=403, detail="Admin only")
        form = await request.form()
        mcp_name = _normalize_mcp_name(str(form.get("name", "")).strip())
        direct_url = _normalize_direct_url(str(form.get("direct_url", "")).strip())
        auth_mode = str(form.get("auth_mode", "broker")).strip().lower()
        if auth_mode not in {"broker", "passthrough"}:
            raise HTTPException(status_code=400, detail="Auth mode must be broker or passthrough")
        required_headers = _parse_required_headers(str(form.get("required_headers", "")))
        static_headers = _parse_static_headers(str(form.get("static_headers", "")))
        url_param_secrets = _parse_url_param_secrets(str(form.get("url_param_secrets", "")))
        auth_type = str(form.get("auth_type", "")).strip() or None
        await _repository(app).upsert_direct_mcp_server(
            McpServerConfiguration(
                name=mcp_name,
                required_headers=required_headers,
                delegated_auth_passthrough=auth_mode == "passthrough",
                auth_type=auth_type,
                source=MCP_SOURCE_DIRECT,
                direct_url=direct_url,
                static_headers=static_headers,
                url_param_secrets=url_param_secrets,
            )
        )
        return RedirectResponse("/admin", status_code=303)

    @app.post("/api/mcp/direct/delete")
    async def delete_direct_mcp(request: Request):
        user = _require_session_user(request)
        if not _is_admin_user(user, settings):
            raise HTTPException(status_code=403, detail="Admin only")
        form = await request.form()
        mcp_name = _normalize_mcp_name(str(form.get("name", "")).strip())
        await _repository(app).delete_direct_mcp_server(mcp_name)
        return RedirectResponse("/admin", status_code=303)

    @app.api_route("/{mcp_name}/authorize", methods=["GET"])
    async def delegated_authorize(request: Request, mcp_name: str):
        return await _handle_delegated_oauth_endpoint(request, _normalize_mcp_name(mcp_name), "authorize")

    @app.api_route("/{mcp_name}/token", methods=["POST"])
    async def delegated_token(request: Request, mcp_name: str):
        return await _handle_delegated_oauth_endpoint(request, _normalize_mcp_name(mcp_name), "token")

    @app.api_route("/{mcp_name}/register", methods=["POST"])
    async def delegated_register(request: Request, mcp_name: str):
        return await _handle_delegated_oauth_endpoint(request, _normalize_mcp_name(mcp_name), "register")

    @app.api_route("/{mcp_name}", methods=["GET", "POST", "DELETE"])
    async def named_mcp_root(request: Request, mcp_name: str):
        return await _handle_mcp(request, _normalize_mcp_name(mcp_name), "")

    @app.api_route("/{mcp_name}/{subpath:path}", methods=["GET", "POST", "DELETE"])
    async def named_mcp_subpath(request: Request, mcp_name: str, subpath: str):
        return await _handle_mcp(request, _normalize_mcp_name(mcp_name), subpath)

    async def _handle_mcp(request: Request, mcp_name: str, subpath: str):
        server = await _repository(app).get_mcp_server(mcp_name)
        if server and server.delegated_auth_passthrough:
            if server.source == MCP_SOURCE_DIRECT:
                return await proxy_direct_passthrough_mcp_request(
                    request=request,
                    server=server,
                    subpath=subpath,
                    settings=settings,
                    http_client=_http_client(app),
                )
            return await proxy_delegated_mcp_request(
                request=request,
                mcp_name=mcp_name,
                subpath=subpath,
                settings=settings,
                http_client=_http_client(app),
            )

        token = _bearer_token(request.headers)
        if token is None:
            return _oauth_challenge(settings, mcp_name, subpath)
        try:
            claims = app.state.jwt_validator.verify(token)
        except JwtValidationError:
            return _oauth_challenge(settings, mcp_name, subpath)

        await _repository(app).upsert_user(claims["sub"], claims.get("email"))
        if not app.state.rate_limiter.allow(claims["sub"]):
            raise HTTPException(status_code=429, detail="Too many MCP requests. Try again later.")
        if server and server.source == MCP_SOURCE_DIRECT:
            return await proxy_direct_broker_mcp_request(
                request=request,
                server=server,
                subpath=subpath,
                user_sub=claims["sub"],
                repository=_repository(app),
                http_client=_http_client(app),
            )
        return await proxy_mcp_request(
            request=request,
            mcp_name=mcp_name,
            subpath=subpath,
            user_sub=claims["sub"],
            settings=settings,
            repository=_repository(app),
            http_client=_http_client(app),
        )

    async def _handle_delegated_oauth_endpoint(request: Request, mcp_name: str, endpoint: str):
        server = await _repository(app).get_mcp_server(mcp_name)
        if not server or not server.delegated_auth_passthrough:
            return await _handle_mcp(request, mcp_name, endpoint)
        if server.source == MCP_SOURCE_DIRECT:
            return await proxy_direct_oauth_endpoint_request(
                request=request,
                server=server,
                endpoint=endpoint,
                settings=settings,
                http_client=_http_client(app),
            )
        return await proxy_delegated_litellm_request(
            request=request,
            path=f"/{mcp_name}/{endpoint}",
            settings=settings,
            http_client=_http_client(app),
        )

    return app


def _oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="pocket_id",
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_id=settings.ui_oidc_client_id,
        client_secret=settings.ui_oidc_client_secret,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def _mcp_server_configurations(servers: list[Any]) -> list[McpServerConfiguration]:
    return [
        McpServerConfiguration(
            name=server.name,
            required_headers=server.required_headers,
            delegated_auth_passthrough=server.delegated_auth_passthrough,
            auth_type=server.auth_type,
        )
        for server in servers
    ]


def _is_admin_user(user: Mapping[str, str], settings: Settings) -> bool:
    return str(user.get("email") or "").lower() in settings.admin_emails


def _is_valid_secret_header_for_server(header_name: str, server: Any | None) -> bool:
    if server is not None and server.source == MCP_SOURCE_DIRECT:
        return is_valid_secret_header_name(header_name)
    return is_valid_litellm_secret_header_name(header_name)


def _bearer_token(headers: Mapping[str, str]) -> str | None:
    value = headers.get("authorization")
    if not value or not value.lower().startswith("bearer "):
        return None
    return value.split(" ", 1)[1].strip()


def _oauth_challenge(settings: Settings, mcp_name: str, subpath: str = "") -> JSONResponse:
    metadata_url = _protected_resource_metadata_url(settings, mcp_name, subpath)
    return JSONResponse(
        {"detail": "OAuth bearer token required"},
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'},
    )


def _normalize_mcp_name(value: str) -> str:
    normalized = value.strip().strip("/")
    if normalized.lower() in RESERVED_MCP_NAMES:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if not MCP_NAME_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="MCP name must use letters, numbers, dot, dash, or underscore")
    return normalized


def _normalize_mcp_subpath(value: str) -> str:
    normalized = value.strip().strip("/")
    if not normalized:
        raise HTTPException(status_code=404, detail="MCP server not found")
    segments = normalized.split("/")
    if any(not MCP_NAME_RE.fullmatch(segment) for segment in segments):
        raise HTTPException(
            status_code=400,
            detail="MCP subpath must use letters, numbers, dot, dash, or underscore",
        )
    return "/".join(segments)


def _public_mcp_resource(settings: Settings, mcp_name: str, subpath: str = "") -> str:
    resource = f"{settings.public_url}/{mcp_name}"
    if subpath:
        resource = f"{resource}/{_normalize_mcp_subpath(subpath)}"
    return resource


def _protected_resource_metadata_url(settings: Settings, mcp_name: str, subpath: str = "") -> str:
    metadata_url = f"{settings.public_url}/.well-known/oauth-protected-resource/{mcp_name}"
    if subpath:
        metadata_url = f"{metadata_url}/{_normalize_mcp_subpath(subpath)}"
    return metadata_url


def _normalize_direct_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Direct MCP URL must be an absolute http or https URL")
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="Direct MCP URL must not include query strings or fragments")
    return value.rstrip("/")


def _parse_required_headers(value: str) -> tuple[str, ...]:
    headers = tuple(normalize_secret_header_name(header) for header in value.split(","))
    required_headers = tuple(header for header in headers if header)
    invalid = [header for header in required_headers if not is_valid_secret_header_name(header)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid required header: {invalid[0]}")
    return tuple(sorted(set(required_headers)))


def _parse_static_headers(value: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise HTTPException(status_code=400, detail="Static headers must use 'Header-Name: value' lines")
        name, header_value = stripped.split(":", 1)
        header_name = normalize_secret_header_name(name)
        if not is_valid_secret_header_name(header_name):
            raise HTTPException(status_code=400, detail=f"Invalid static header: {header_name}")
        if not header_value.strip():
            raise HTTPException(status_code=400, detail=f"Static header {header_name} requires a value")
        headers[header_name] = header_value.strip()
    return dict(sorted(headers.items()))


def _parse_url_param_secrets(value: str) -> tuple[str, ...]:
    params = tuple(normalize_secret_header_name(param) for param in value.split(","))
    url_param_secrets = tuple(param for param in params if param)
    invalid = [param for param in url_param_secrets if not is_valid_secret_header_name(param)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid URL param secret: {invalid[0]}")
    return tuple(sorted(set(url_param_secrets)))


def _session_user(request: Request) -> dict[str, str] | None:
    user = request.session.get("user")
    return user if isinstance(user, dict) and "sub" in user else None


def _require_session_user(request: Request) -> dict[str, str]:
    user = _session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def _repository(app: FastAPI) -> Repository:
    return app.state.repository


def _http_client(app: FastAPI) -> httpx.AsyncClient:
    return app.state.http_client


async def _mcp_tool_health_statuses(
    app: FastAPI,
    settings: Settings,
    user_sub: str,
    servers: list[Any],
) -> dict[str, McpToolHealth]:
    http_client = getattr(app.state, "http_client", None)
    if http_client is None:
        return {
            server.name: health_unknown("Health check unavailable until the broker HTTP client is ready.")
            for server in servers
        }
    return await McpToolsHealthClient(settings, http_client).check_servers(
        user_sub=user_sub,
        repository=_repository(app),
        servers=servers,
    )


def _ensure_mcp_server_catalog_columns(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("mcp_servers")}
    if "source" not in columns:
        connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'litellm'"))
    if "direct_url" not in columns:
        connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN direct_url TEXT"))
    if "static_headers_json" not in columns:
        connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN static_headers_json TEXT NOT NULL DEFAULT '{}'"))
    if "url_param_secrets_json" not in columns:
        connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN url_param_secrets_json TEXT NOT NULL DEFAULT '[]'"))
    if "active" not in columns:
        connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE"))
