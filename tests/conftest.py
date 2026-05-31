from collections.abc import Generator

import pytest
from cryptography.fernet import Fernet

from mcp_broker.config import Settings


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
        expected_audience="claude-connector-client-id",
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
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.litellm_key = litellm_key
        self.secrets = secrets or {"X-DOKPLOY-TOKEN": "dokploy-user-token"}
        self.users: list[tuple[str, str | None]] = []

    async def upsert_user(self, sub: str, email: str | None = None) -> None:
        self.users.append((sub, email))

    async def get_litellm_key(self, user_sub: str) -> str | None:
        assert user_sub == "pocket-sub"
        return self.litellm_key

    async def get_secrets(self, user_sub: str) -> dict[str, str]:
        assert user_sub == "pocket-sub"
        return self.secrets


@pytest.fixture
def fake_repository() -> Generator[FakeRepository]:
    yield FakeRepository()
