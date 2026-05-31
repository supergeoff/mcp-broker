from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import pytest

from mcp_broker.models import Base, UserLiteLLMKey
from mcp_broker.security import FernetCipher
from mcp_broker.storage import VaultRepository

pytestmark = pytest.mark.anyio


async def test_vault_repository_encrypts_and_upserts_user_values(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_user("pocket-sub", "user@example.com")
    await repository.upsert_litellm_key("pocket-sub", "litellm-user-key")
    await repository.upsert_secret("pocket-sub", "X-DOKPLOY-TOKEN", "old-token")
    await repository.upsert_secret("pocket-sub", "X-DOKPLOY-TOKEN", "new-token")

    assert await repository.get_litellm_key("pocket-sub") == "litellm-user-key"
    assert await repository.get_secrets("pocket-sub") == {"X-DOKPLOY-TOKEN": "new-token"}

    async with session_factory() as session:
        stored_key = await session.scalar(
            select(UserLiteLLMKey).where(UserLiteLLMKey.user_sub == "pocket-sub")
        )

    assert stored_key is not None
    assert "litellm-user-key" not in stored_key.enc_value

    await engine.dispose()
