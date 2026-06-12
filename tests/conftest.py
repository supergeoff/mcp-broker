from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet

from mcp_broker.config import Settings
from mcp_broker.storage import McpServerConfiguration
from mcp_broker.storage import UserConfigurationState


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def settings(encryption_key: str) -> Settings:
    return Settings(
        public_url="https://broker.example.com",
        database_url="sqlite+aiosqlite:///:memory:",
        secrets_encryption_key=encryption_key,
        session_secret="session-secret-for-tests",
        oidc_issuer="https://id.example.com",
        jwks_uri="https://id.example.com/oauth2/jwks",
        ui_oidc_client_id="ui-client-id",
        ui_oidc_client_secret="ui-client-secret",
        expected_issuer="https://id.example.com",
        expected_audience="broker-mcp-client",
        litellm_base_url="https://litellm.example.com",
        litellm_admin_key="admin-read-key",
        admin_emails="admin@example.com",
    )


class FakeJwtValidator:
    def __init__(self, sub: str = "pocket-sub") -> None:
        self.sub = sub

    def verify(self, token: str) -> dict[str, str]:
        assert token == "oauth-access-token"
        return {"sub": self.sub, "email": "user@example.com"}


class FakeRepository:
    def __init__(
        self,
        litellm_key: str | None = "litellm-user-key",
        secrets: dict[str, dict[str, str]] | None = None,
        mcp_servers: dict[str, McpServerConfiguration] | None = None,
    ) -> None:
        self.litellm_key = litellm_key
        self.secrets = secrets or {
            "dokploy": {"X-DOKPLOY-TOKEN": "dokploy-user-token"},
        }
        self.mcp_servers = mcp_servers or {}
        self.users: list[tuple[str, str | None]] = []

    async def upsert_user(self, sub: str, email: str | None = None) -> None:
        self.users.append((sub, email))

    async def get_litellm_key(self, user_sub: str) -> str | None:
        assert user_sub == "pocket-sub"
        return self.litellm_key

    async def upsert_secret(self, user_sub: str, mcp_name: str, header_name: str, value: str) -> None:
        assert user_sub == "pocket-sub"
        self.secrets.setdefault(mcp_name, {})[header_name] = value

    async def delete_secret(self, user_sub: str, mcp_name: str, header_name: str) -> None:
        assert user_sub == "pocket-sub"
        headers = self.secrets.get(mcp_name)
        if headers is None:
            return
        headers.pop(header_name, None)
        if not headers:
            self.secrets.pop(mcp_name, None)

    async def delete_mcp_secrets(self, user_sub: str, mcp_name: str) -> None:
        assert user_sub == "pocket-sub"
        self.secrets.pop(mcp_name, None)

    async def get_secrets(self, user_sub: str, mcp_name: str) -> dict[str, str]:
        assert user_sub == "pocket-sub"
        return self.secrets.get(mcp_name, {})

    async def list_secret_headers(self, user_sub: str) -> dict[str, tuple[str, ...]]:
        assert user_sub == "pocket-sub"
        return {mcp_name: tuple(headers) for mcp_name, headers in self.secrets.items()}

    async def upsert_mcp_servers(self, servers: list[McpServerConfiguration]) -> None:
        active_names: set[str] = set()
        for server in servers:
            active_names.add(server.name)
            existing = self.mcp_servers.get(server.name)
            if existing is not None and existing.source == "direct":
                continue
            self.mcp_servers[server.name] = McpServerConfiguration(
                name=server.name,
                required_headers=tuple(sorted(server.required_headers)),
                delegated_auth_passthrough=server.delegated_auth_passthrough,
                auth_type=server.auth_type,
                source="litellm",
                direct_url=None,
                static_headers={},
                active=True,
            )
        for name, server in list(self.mcp_servers.items()):
            if server.source == "litellm" and name not in active_names:
                self.mcp_servers[name] = McpServerConfiguration(
                    name=server.name,
                    required_headers=server.required_headers,
                    delegated_auth_passthrough=server.delegated_auth_passthrough,
                    auth_type=server.auth_type,
                    source=server.source,
                    direct_url=server.direct_url,
                    static_headers=server.static_headers,
                    active=False,
                )

    async def upsert_direct_mcp_server(self, server: McpServerConfiguration) -> None:
        self.mcp_servers[server.name] = McpServerConfiguration(
            name=server.name,
            required_headers=tuple(sorted(server.required_headers)),
            delegated_auth_passthrough=server.delegated_auth_passthrough,
            auth_type=server.auth_type,
            source="direct",
            direct_url=server.direct_url.rstrip("/") if server.direct_url else None,
            static_headers=dict(server.static_headers),
            active=True,
        )

    async def delete_direct_mcp_server(self, mcp_name: str) -> None:
        existing = self.mcp_servers.get(mcp_name)
        if existing is not None and existing.source == "direct":
            self.mcp_servers[mcp_name] = McpServerConfiguration(
                name=existing.name,
                required_headers=existing.required_headers,
                delegated_auth_passthrough=existing.delegated_auth_passthrough,
                auth_type=existing.auth_type,
                source=existing.source,
                direct_url=existing.direct_url,
                static_headers=existing.static_headers,
                active=False,
            )

    async def delete_mcp_server(self, mcp_name: str) -> None:
        self.mcp_servers.pop(mcp_name, None)

    async def get_mcp_server(self, mcp_name: str) -> McpServerConfiguration | None:
        server = self.mcp_servers.get(mcp_name)
        return server if server and server.active else None

    async def list_mcp_servers(self) -> list[McpServerConfiguration]:
        return sorted(self.mcp_servers.values(), key=lambda server: server.name)

    async def set_mcp_delegated_auth(self, mcp_name: str, enabled: bool) -> None:
        existing = self.mcp_servers.get(mcp_name)
        self.mcp_servers[mcp_name] = McpServerConfiguration(
            name=mcp_name,
            required_headers=existing.required_headers if existing else (),
            delegated_auth_passthrough=enabled,
            auth_type=existing.auth_type if existing else None,
            source=existing.source if existing else "litellm",
            direct_url=existing.direct_url if existing else None,
            static_headers=existing.static_headers if existing else {},
            active=existing.active if existing else True,
        )

    async def list_user_states(self) -> list[UserConfigurationState]:
        return [
            UserConfigurationState(
                sub="pocket-sub",
                email="admin@example.com",
                has_litellm_key=self.litellm_key is not None,
                secret_count=sum(len(headers) for headers in self.secrets.values()),
            )
        ]


@pytest.fixture
def fake_repository() -> Generator[FakeRepository]:
    yield FakeRepository()
