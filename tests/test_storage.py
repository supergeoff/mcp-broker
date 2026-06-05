from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import pytest

from mcp_broker.models import Base, UserLiteLLMKey
from mcp_broker.security import FernetCipher
from mcp_broker.storage import McpServerConfiguration, VaultRepository

pytestmark = pytest.mark.anyio


async def test_vault_repository_encrypts_and_upserts_user_values(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_user("pocket-sub", "user@example.com")
    await repository.upsert_litellm_key("pocket-sub", "litellm-user-key")
    await repository.upsert_secret("pocket-sub", "dokploy", "X-DOKPLOY-TOKEN", "old-token")
    await repository.upsert_secret("pocket-sub", "dokploy", "X-DOKPLOY-TOKEN", "new-token")
    await repository.upsert_secret("pocket-sub", "context7", "X-DOKPLOY-TOKEN", "context-token")

    assert await repository.get_litellm_key("pocket-sub") == "litellm-user-key"
    assert await repository.get_secrets("pocket-sub", "dokploy") == {"X-DOKPLOY-TOKEN": "new-token"}
    assert await repository.get_secrets("pocket-sub", "context7") == {"X-DOKPLOY-TOKEN": "context-token"}
    assert await repository.list_secret_headers("pocket-sub") == {
        "context7": ("X-DOKPLOY-TOKEN",),
        "dokploy": ("X-DOKPLOY-TOKEN",),
    }

    async with session_factory() as session:
        stored_key = await session.scalar(
            select(UserLiteLLMKey).where(UserLiteLLMKey.user_sub == "pocket-sub")
        )

    assert stored_key is not None
    assert "litellm-user-key" not in stored_key.enc_value

    await engine.dispose()


async def test_vault_repository_deletes_secret_headers(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_secret("pocket-sub", "dokploy", "X-DOKPLOY-TOKEN", "dokploy-token")
    await repository.upsert_secret("pocket-sub", "dokploy", "X-DOKPLOY-ORG", "dokploy-org")
    await repository.upsert_secret("pocket-sub", "github", "X-GITHUB-TOKEN", "github-token")

    await repository.delete_secret("pocket-sub", "dokploy", "X-DOKPLOY-TOKEN")

    assert await repository.get_secrets("pocket-sub", "dokploy") == {"X-DOKPLOY-ORG": "dokploy-org"}
    assert await repository.get_secrets("pocket-sub", "github") == {"X-GITHUB-TOKEN": "github-token"}

    await repository.delete_mcp_secrets("pocket-sub", "dokploy")

    assert await repository.get_secrets("pocket-sub", "dokploy") == {}
    assert await repository.get_secrets("pocket-sub", "github") == {"X-GITHUB-TOKEN": "github-token"}

    await engine.dispose()


async def test_vault_repository_upserts_discovered_mcp_server_configuration(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_mcp_servers(
        [
            McpServerConfiguration(
                name="github",
                required_headers=("X-GITHUB-TOKEN",),
                delegated_auth_passthrough=True,
                auth_type="oauth2",
            )
        ]
    )
    await repository.upsert_mcp_servers(
        [
            McpServerConfiguration(
                name="github",
                required_headers=("X-GITHUB-TOKEN", "X-GITHUB-ORG"),
                delegated_auth_passthrough=False,
                auth_type="bearer_token",
            )
        ]
    )

    assert await repository.get_mcp_server("github") == McpServerConfiguration(
        name="github",
        required_headers=("X-GITHUB-ORG", "X-GITHUB-TOKEN"),
        delegated_auth_passthrough=False,
        auth_type="bearer_token",
    )
    assert await repository.list_mcp_servers() == [
        McpServerConfiguration(
            name="github",
            required_headers=("X-GITHUB-ORG", "X-GITHUB-TOKEN"),
            delegated_auth_passthrough=False,
            auth_type="bearer_token",
        )
    ]

    await engine.dispose()
