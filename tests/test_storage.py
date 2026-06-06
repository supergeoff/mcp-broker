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


async def test_vault_repository_persists_direct_mcp_server_configuration(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_direct_mcp_server(
        McpServerConfiguration(
            name="googlemcp",
            required_headers=("X-GOOGLE-WORKSPACE",),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp/",
        )
    )

    assert await repository.get_mcp_server("googlemcp") == McpServerConfiguration(
        name="googlemcp",
        required_headers=("X-GOOGLE-WORKSPACE",),
        delegated_auth_passthrough=True,
        auth_type="oauth2",
        source="direct",
        direct_url="https://googlemcp.example.com/mcp",
    )
    assert await repository.list_mcp_servers() == [
        McpServerConfiguration(
            name="googlemcp",
            required_headers=("X-GOOGLE-WORKSPACE",),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp",
        )
    ]

    await engine.dispose()


async def test_vault_repository_does_not_overwrite_direct_server_with_litellm_discovery(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_direct_mcp_server(
        McpServerConfiguration(
            name="googlemcp",
            required_headers=(),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp",
        )
    )
    await repository.upsert_mcp_servers(
        [
            McpServerConfiguration(
                name="googlemcp",
                required_headers=("X-LITELLM-HEADER",),
                delegated_auth_passthrough=False,
                auth_type="bearer_token",
                source="litellm",
            )
        ]
    )

    assert await repository.get_mcp_server("googlemcp") == McpServerConfiguration(
        name="googlemcp",
        required_headers=(),
        delegated_auth_passthrough=True,
        auth_type="oauth2",
        source="direct",
        direct_url="https://googlemcp.example.com/mcp",
    )

    await engine.dispose()


async def test_vault_repository_retires_litellm_servers_missing_from_discovery(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_mcp_servers(
        [
            McpServerConfiguration(
                name="daytona",
                required_headers=("X-DAYTONA-TOKEN",),
                delegated_auth_passthrough=False,
                auth_type="bearer_token",
                source="litellm",
            ),
            McpServerConfiguration(
                name="github",
                required_headers=(),
                delegated_auth_passthrough=True,
                auth_type="oauth2",
                source="litellm",
            )
        ]
    )
    await repository.upsert_mcp_servers(
        [
            McpServerConfiguration(
                name="github",
                required_headers=(),
                delegated_auth_passthrough=True,
                auth_type="oauth2",
                source="litellm",
            )
        ]
    )

    assert await repository.list_mcp_servers() == [
        McpServerConfiguration(
            name="daytona",
            required_headers=("X-DAYTONA-TOKEN",),
            delegated_auth_passthrough=False,
            auth_type="bearer_token",
            source="litellm",
            direct_url=None,
            active=False,
        ),
        McpServerConfiguration(
            name="github",
            required_headers=(),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="litellm",
            direct_url=None,
            active=True,
        )
    ]
    assert await repository.get_mcp_server("daytona") is None

    await engine.dispose()


async def test_vault_repository_retires_direct_mcp_server(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_direct_mcp_server(
        McpServerConfiguration(
            name="googlemcp",
            required_headers=(),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp",
        )
    )

    await repository.delete_direct_mcp_server("googlemcp")

    assert await repository.get_mcp_server("googlemcp") is None
    assert await repository.list_mcp_servers() == [
        McpServerConfiguration(
            name="googlemcp",
            required_headers=(),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="direct",
            direct_url="https://googlemcp.example.com/mcp",
            active=False,
        )
    ]

    await engine.dispose()


async def test_vault_repository_removes_retired_mcp_server_without_deleting_secrets(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_secret("pocket-sub", "daytona", "X-DAYTONA-TOKEN", "daytona-token")
    await repository.upsert_mcp_servers(
        [
            McpServerConfiguration(
                name="daytona",
                required_headers=("X-DAYTONA-TOKEN",),
                source="litellm",
            )
        ]
    )
    await repository.upsert_mcp_servers([])

    await repository.delete_mcp_server("daytona")

    assert await repository.list_mcp_servers() == []
    assert await repository.get_secrets("pocket-sub", "daytona") == {"X-DAYTONA-TOKEN": "daytona-token"}

    await engine.dispose()
