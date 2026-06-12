import json
from base64 import b64encode
from html.parser import HTMLParser

import httpx
import pytest
from itsdangerous import TimestampSigner

from mcp_broker.app import create_app
from mcp_broker.storage import McpServerConfiguration

pytestmark = pytest.mark.anyio


class SecretInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.password_inputs: dict[str, dict[str, str | None]] = {}
        self.toggle_controls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("type") == "password":
            input_id = attributes.get("id")
            if input_id is not None:
                self.password_inputs[input_id] = attributes
        if tag == "button" and "data-secret-toggle" in attributes:
            controls = attributes.get("aria-controls")
            if controls is not None:
                self.toggle_controls.append(controls)


def _session_cookie(secret: str, payload: dict[str, object]) -> str:
    data = b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(secret).sign(data).decode("utf-8")


async def test_dashboard_renders_after_successful_login(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert "admin@example.com" in response.text


async def test_dashboard_saves_secret_header_for_named_mcp(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/secret",
            data={
                "mcp_name": "dokploy",
                "header_name": "X-DOKPLOY_API_KEY",
                "value": "dokploy-key",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.secrets["dokploy"]["X-DOKPLOY_API_KEY"] == "dokploy-key"


async def test_dashboard_saves_valid_custom_secret_header_for_named_mcp(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/secret",
            data={
                "mcp_name": "dokploy",
                "header_name": "xc-mcp-token",
                "value": "mcp-secret",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.secrets["dokploy"]["xc-mcp-token"] == "mcp-secret"


async def test_dashboard_saves_authorization_secret_header_for_named_mcp(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/secret",
            data={
                "mcp_name": "dokploy",
                "header_name": "Authorization",
                "value": "Bearer upstream-token",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.secrets["dokploy"]["Authorization"] == "Bearer upstream-token"


async def test_dashboard_rejects_litellm_api_key_as_secret_header(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/secret",
            data={
                "mcp_name": "dokploy",
                "header_name": "x-litellm-api-key",
                "value": "Bearer litellm-user-key",
            },
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "x-litellm-api-key" not in fake_repository.secrets["dokploy"]


async def test_dashboard_deletes_secret_header_for_named_mcp(settings, fake_repository) -> None:
    fake_repository.secrets = {
        "dokploy": {
            "X-DOKPLOY-TOKEN": "dokploy-token",
            "X-DOKPLOY-ORG": "dokploy-org",
        },
        "github": {"X-GITHUB-TOKEN": "github-token"},
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/secret/delete",
            data={
                "mcp_name": "dokploy",
                "header_name": "X-DOKPLOY-TOKEN",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.secrets == {
        "dokploy": {"X-DOKPLOY-ORG": "dokploy-org"},
        "github": {"X-GITHUB-TOKEN": "github-token"},
    }


async def test_dashboard_deletes_authorization_secret_header_for_named_mcp(settings, fake_repository) -> None:
    fake_repository.secrets = {
        "dokploy": {
            "Authorization": "Bearer upstream-token",
            "X-DOKPLOY-ORG": "dokploy-org",
        }
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/secret/delete",
            data={
                "mcp_name": "dokploy",
                "header_name": "Authorization",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.secrets == {"dokploy": {"X-DOKPLOY-ORG": "dokploy-org"}}


async def test_dashboard_deletes_all_secret_headers_for_named_mcp(settings, fake_repository) -> None:
    fake_repository.secrets = {
        "dokploy": {
            "X-DOKPLOY-TOKEN": "dokploy-token",
            "X-DOKPLOY-ORG": "dokploy-org",
        },
        "github": {"X-GITHUB-TOKEN": "github-token"},
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/mcp/secrets/delete",
            data={"mcp_name": "dokploy"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.secrets == {"github": {"X-GITHUB-TOKEN": "github-token"}}


async def test_dashboard_renders_delete_controls_for_saved_headers(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "dokploy": McpServerConfiguration(
            name="dokploy",
            required_headers=("X-DOKPLOY-TOKEN", "X-DOKPLOY-ORG"),
            delegated_auth_passthrough=False,
        )
    }
    fake_repository.secrets = {
        "dokploy": {
            "X-DOKPLOY-TOKEN": "dokploy-token",
            "X-DOKPLOY-ORG": "dokploy-org",
        }
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert 'action="/api/secret/delete"' in response.text
    assert 'action="/api/mcp/secrets/delete"' in response.text
    assert 'name="mcp_name" value="dokploy"' in response.text
    assert 'name="header_name" value="X-DOKPLOY-TOKEN"' in response.text
    assert 'name="header_name" value="X-DOKPLOY-ORG"' in response.text
    assert "Delete all" in response.text


async def test_dashboard_uses_dokploy_shell_and_system_theme(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert 'class="app-shell"' in response.text
    assert "@media (prefers-color-scheme: dark)" in response.text
    assert "Dashboard" in response.text
    assert "MCP discovery" in response.text
    assert "admin@example.com" in response.text


async def test_dashboard_renders_light_dark_system_theme_toggle(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert 'class="theme-toggle"' in response.text
    assert 'data-theme-choice="light"' in response.text
    assert 'data-theme-choice="dark"' in response.text
    assert 'data-theme-choice="system"' in response.text
    assert "mcp-broker-theme" in response.text
    assert 'html[data-theme="light"]' in response.text
    assert 'html[data-theme="dark"]' in response.text


async def test_dashboard_renders_discovered_mcp_server_boxes_from_storage(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "github": McpServerConfiguration(
            name="github",
            required_headers=("X-GITHUB-TOKEN",),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
        )
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert 'class="server-card"' in response.text
    assert "github" in response.text
    assert "X-GITHUB-TOKEN" in response.text
    assert 'name="delegated_auth_passthrough"' in response.text
    assert "PKCE passthrough" in response.text


async def test_dashboard_renders_direct_mcp_in_catalog(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "googlemcp": McpServerConfiguration(
            name="googlemcp",
            required_headers=(),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp",
        )
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert "googlemcp" in response.text
    assert "Direct" in response.text
    assert "OAuth direct" in response.text
    assert "No client ID or client secret needed in Claude or OpenWebUI." in response.text
    assert "Health unknown" in response.text


async def test_dashboard_renders_mcp_tools_health_from_listed_tools(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "dokploy": McpServerConfiguration(
            name="dokploy",
            required_headers=("X-DOKPLOY_API_KEY",),
            delegated_auth_passthrough=False,
            source="litellm",
        )
    }
    fake_repository.secrets = {"dokploy": {"X-DOKPLOY_API_KEY": "dokploy-key"}}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads((await request.aread()).decode("utf-8"))
        if body["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "dokploy.deploy"}]},
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        app = create_app(settings=settings, repository=fake_repository, http_client=http_client)
        cookie = _session_cookie(
            settings.session_secret,
            {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            client.cookies.set("session", cookie)
            response = await client.get("/")

    assert response.status_code == 200
    assert "dokploy" in response.text
    assert "Tools healthy" in response.text
    assert "1 tool listed by tools/list." in response.text


async def test_dashboard_hides_saved_headers_for_servers_not_in_catalog(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {}
    fake_repository.secrets = {
        "googlemcp": {"X-GOOGLE-WORKSPACE": "workspace-token"},
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert "googlemcp" not in response.text
    assert "X-GOOGLE-WORKSPACE" not in response.text


async def test_dashboard_renders_retired_mcp_servers_with_admin_remove_control(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "daytona": McpServerConfiguration(
            name="daytona",
            required_headers=("X-DAYTONA-TOKEN",),
            delegated_auth_passthrough=False,
            auth_type="bearer_token",
            source="litellm",
            active=False,
        ),
        "github": McpServerConfiguration(
            name="github",
            required_headers=("X-GITHUB-TOKEN",),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="litellm",
            active=True,
        ),
    }
    fake_repository.secrets = {
        "daytona": {"X-DAYTONA-TOKEN": "daytona-token"},
        "github": {"X-GITHUB-TOKEN": "github-token"},
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert 'class="server-card server-card-retired"' in response.text
    assert "Retired MCP servers" in response.text
    assert "daytona" in response.text
    assert "No longer in LiteLLM" in response.text
    assert 'action="/api/mcp/remove"' in response.text
    assert 'name="mcp_name" value="daytona"' in response.text
    assert "X-DAYTONA-TOKEN" not in response.text
    assert "github" in response.text
    assert "X-GITHUB-TOKEN" in response.text


async def test_dashboard_removes_retired_mcp_catalog_entry_without_deleting_secrets(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "daytona": McpServerConfiguration(
            name="daytona",
            required_headers=("X-DAYTONA-TOKEN",),
            source="litellm",
            active=False,
        )
    }
    fake_repository.secrets = {
        "daytona": {"X-DAYTONA-TOKEN": "daytona-token"},
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/mcp/remove",
            data={"mcp_name": "daytona"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.mcp_servers == {}
    assert fake_repository.secrets == {"daytona": {"X-DAYTONA-TOKEN": "daytona-token"}}


async def test_non_admin_cannot_remove_retired_mcp_catalog_entry(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "daytona": McpServerConfiguration(
            name="daytona",
            required_headers=(),
            source="litellm",
            active=False,
        )
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "user@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/mcp/remove",
            data={"mcp_name": "daytona"},
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert "daytona" in fake_repository.mcp_servers


async def test_dashboard_renders_reveal_controls_for_every_secret_input(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "github": McpServerConfiguration(
            name="github",
            required_headers=("X-GITHUB-TOKEN",),
            delegated_auth_passthrough=False,
            auth_type="bearer_token",
        )
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    parser = SecretInputParser()
    parser.feed(response.text)

    assert response.status_code == 200
    assert set(parser.password_inputs) == {"litellm_key", "secret-github-1", "manual-secret-github"}
    assert set(parser.toggle_controls) == set(parser.password_inputs)
    assert all("data-secret-input" in attributes for attributes in parser.password_inputs.values())
    assert "toggleBrokerSecret" in response.text
    assert "Show secret value" in response.text
    assert "Hide secret value" in response.text


async def test_dashboard_renders_common_header_suggestions_without_litellm_api_key(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "github": McpServerConfiguration(
            name="github",
            required_headers=(),
            delegated_auth_passthrough=False,
        )
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/")

    assert response.status_code == 200
    assert 'id="common-secret-headers"' in response.text
    assert 'list="common-secret-headers"' in response.text
    assert 'value="Authorization"' in response.text
    assert 'value="X-API-Key"' in response.text
    assert "x-litellm-api-key" not in response.text


async def test_admin_uses_dokploy_shell_and_status_badges(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/admin")

    assert response.status_code == 200
    assert 'class="app-shell"' in response.text
    assert 'class="data-table"' in response.text
    assert 'class="status-badge status-saved"' in response.text
    assert "admin@example.com" in response.text


async def test_admin_direct_mcp_form_uses_neutral_placeholders(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/admin")

    assert response.status_code == 200
    assert 'placeholder="internal-mcp"' in response.text
    assert 'placeholder="https://mcp.example.com/mcp"' in response.text
    assert "googlemcp" not in response.text
    assert "googlemcp.supergeoff.top" not in response.text


async def test_admin_renders_litellm_upstream_health_check(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/admin")

    assert response.status_code == 200
    assert "LiteLLM upstream health" in response.text
    assert 'hx-post="/api/litellm/upstream-health"' in response.text
    assert 'hx-target="#litellm-upstream-health-results"' in response.text
    assert "htmx.org" in response.text


async def test_admin_renders_direct_mcp_form_and_entries(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "googlemcp": McpServerConfiguration(
            name="googlemcp",
            required_headers=("X-GOOGLE-WORKSPACE",),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp",
        )
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.get("/admin")

    assert response.status_code == 200
    assert 'action="/api/mcp/direct"' in response.text
    assert 'name="name"' in response.text
    assert 'name="direct_url"' in response.text
    assert 'name="auth_mode"' in response.text
    assert 'name="static_headers"' in response.text
    assert "googlemcp" in response.text
    assert "https://googlemcp.example.com/mcp" in response.text
    assert 'action="/api/mcp/direct/delete"' in response.text


async def test_admin_adds_direct_passthrough_mcp(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/mcp/direct",
            data={
                "name": "googlemcp",
                "direct_url": "https://googlemcp.example.com/mcp/",
                "auth_mode": "passthrough",
                "auth_type": "oauth2",
                "required_headers": "Authorization, X-GOOGLE-WORKSPACE, X-GOOGLE-ORG",
                "static_headers": "X-UPSTREAM-TOKEN: upstream-secret",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.mcp_servers["googlemcp"] == McpServerConfiguration(
        name="googlemcp",
        required_headers=("Authorization", "X-GOOGLE-ORG", "X-GOOGLE-WORKSPACE"),
        delegated_auth_passthrough=True,
        auth_type="oauth2",
        source="direct",
        direct_url="https://googlemcp.example.com/mcp",
        static_headers={"X-UPSTREAM-TOKEN": "upstream-secret"},
    )


async def test_admin_deletes_direct_mcp(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "googlemcp": McpServerConfiguration(
            name="googlemcp",
            required_headers=(),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp",
        )
    }
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post(
            "/api/mcp/direct/delete",
            data={"name": "googlemcp"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.mcp_servers["googlemcp"].active is False


async def test_non_admin_cannot_add_or_delete_direct_mcp(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "user@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        add_response = await client.post(
            "/api/mcp/direct",
            data={
                "name": "googlemcp",
                "direct_url": "https://googlemcp.example.com/mcp",
                "auth_mode": "passthrough",
            },
        )
        delete_response = await client.post(
            "/api/mcp/direct/delete",
            data={"name": "googlemcp"},
        )

    assert add_response.status_code == 403
    assert delete_response.status_code == 403
    assert fake_repository.mcp_servers == {}


async def test_admin_checks_litellm_upstream_health(settings, fake_repository) -> None:
    captured: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "healthy_endpoints": [
                    {"model": "gpt-4o-mini", "api_base": "https://api.openai.com/v1"}
                ],
                "unhealthy_endpoints": [
                    {
                        "model": "claude-sonnet",
                        "api_base": "https://api.anthropic.com",
                        "error": "401 upstream auth failed",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as litellm_client:
        app = create_app(settings=settings, repository=fake_repository, http_client=litellm_client)
        cookie = _session_cookie(
            settings.session_secret,
            {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            client.cookies.set("session", cookie)
            response = await client.post("/api/litellm/upstream-health")

    assert response.status_code == 200
    assert captured == {"path": "/health", "authorization": "Bearer admin-read-key"}
    assert "gpt-4o-mini" in response.text
    assert "claude-sonnet" in response.text
    assert "healthy" in response.text
    assert "unhealthy" in response.text
    assert "401 upstream auth failed" in response.text


async def test_admin_shows_empty_litellm_health_response_as_healthy(settings, fake_repository) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "healthy_endpoints": [],
                "unhealthy_endpoints": [],
                "healthy_count": 0,
                "unhealthy_count": 0,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as litellm_client:
        app = create_app(settings=settings, repository=fake_repository, http_client=litellm_client)
        cookie = _session_cookie(
            settings.session_secret,
            {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            client.cookies.set("session", cookie)
            response = await client.post("/api/litellm/upstream-health")

    assert response.status_code == 200
    assert ">healthy<" in response.text
    assert "LiteLLM returned no per-model health details" in response.text
    assert "LiteLLM health returned no model endpoint data" not in response.text


async def test_non_admin_cannot_check_litellm_upstream_health(settings, fake_repository) -> None:
    app = create_app(settings=settings, repository=fake_repository)
    cookie = _session_cookie(
        settings.session_secret,
        {"user": {"sub": "pocket-sub", "email": "user@example.com"}},
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        client.cookies.set("session", cookie)
        response = await client.post("/api/litellm/upstream-health")

    assert response.status_code == 403


async def test_discovery_partial_uses_result_cards_for_named_mcp(settings, fake_repository) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("x-litellm-api-key")
        if request.url.path == "/v1/mcp/server" and auth == "Bearer admin-read-key":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"name": "dokploy", "env": {"TOKEN": "${X-DOKPLOY-TOKEN}"}},
                    ]
                },
            )
        if request.url.path == "/v1/mcp/server" and auth == "Bearer litellm-user-key":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"name": "dokploy", "env": {"TOKEN": "${X-DOKPLOY-TOKEN}"}},
                    ]
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as litellm_client:
        app = create_app(settings=settings, repository=fake_repository, http_client=litellm_client)
        cookie = _session_cookie(
            settings.session_secret,
            {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            client.cookies.set("session", cookie)
            response = await client.post("/api/discover")

    assert response.status_code == 200
    assert 'class="server-card"' in response.text
    assert 'name="mcp_name" value="dokploy"' in response.text
    assert "X-DOKPLOY-TOKEN" in response.text
    assert "Saved" in response.text


async def test_discovery_stores_admin_mcp_catalog_metadata_even_when_user_filter_hides_server(settings, fake_repository) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("x-litellm-api-key")
        if request.url.path == "/v1/mcp/server" and auth == "Bearer admin-read-key":
            return httpx.Response(
                200,
                json=[
                    {
                        "server_name": "github",
                        "auth_type": "oauth2",
                        "delegate_auth_to_upstream": True,
                    },
                    {
                        "server_name": "dokploy",
                        "env": {"TOKEN": "${X-DOKPLOY-TOKEN}"},
                    },
                ],
            )
        if request.url.path == "/v1/mcp/server" and auth == "Bearer litellm-user-key":
            return httpx.Response(403, json={"error": "forbidden"})
        if request.url.path == "/v1/mcp/tools" and auth == "Bearer litellm-user-key":
            return httpx.Response(200, json={"tools": [{"name": "dokploy.deploy"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as litellm_client:
        app = create_app(settings=settings, repository=fake_repository, http_client=litellm_client)
        cookie = _session_cookie(
            settings.session_secret,
            {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            client.cookies.set("session", cookie)
            response = await client.post("/api/discover")

    assert response.status_code == 200
    assert fake_repository.mcp_servers["github"].delegated_auth_passthrough is True
    assert fake_repository.mcp_servers["github"].auth_type == "oauth2"


async def test_discovery_retires_litellm_servers_missing_from_admin_catalog(settings, fake_repository) -> None:
    fake_repository.mcp_servers = {
        "daytona": McpServerConfiguration(
            name="daytona",
            required_headers=("X-DAYTONA-TOKEN",),
            source="litellm",
            active=True,
        )
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("x-litellm-api-key")
        if request.url.path == "/v1/mcp/server" and auth == "Bearer admin-read-key":
            return httpx.Response(200, json={"servers": []})
        if request.url.path == "/v1/mcp/server" and auth == "Bearer litellm-user-key":
            return httpx.Response(200, json={"servers": []})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as litellm_client:
        app = create_app(settings=settings, repository=fake_repository, http_client=litellm_client)
        cookie = _session_cookie(
            settings.session_secret,
            {"user": {"sub": "pocket-sub", "email": "admin@example.com"}},
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client:
            client.cookies.set("session", cookie)
            response = await client.post("/api/discover")

    assert response.status_code == 200
    assert fake_repository.mcp_servers["daytona"].active is False
