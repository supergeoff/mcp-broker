from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mcp_broker.models import User, UserLiteLLMKey, UserSecret
from mcp_broker.security import FernetCipher


class Repository(Protocol):
    async def upsert_user(self, sub: str, email: str | None = None) -> None: ...
    async def get_litellm_key(self, user_sub: str) -> str | None: ...
    async def upsert_secret(self, user_sub: str, mcp_name: str, header_name: str, value: str) -> None: ...
    async def get_secrets(self, user_sub: str, mcp_name: str) -> dict[str, str]: ...
    async def list_secret_headers(self, user_sub: str) -> dict[str, tuple[str, ...]]: ...


@dataclass(frozen=True)
class UserConfigurationState:
    sub: str
    email: str | None
    has_litellm_key: bool
    secret_count: int


class VaultRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], cipher: FernetCipher) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def upsert_user(self, sub: str, email: str | None = None) -> None:
        async with self._session_factory() as session:
            user = await session.get(User, sub)
            if user is None:
                session.add(User(sub=sub, email=email))
            elif email:
                user.email = email
            await session.commit()

    async def upsert_litellm_key(self, user_sub: str, value: str) -> None:
        encrypted = self._cipher.encrypt(value)
        async with self._session_factory() as session:
            await self._ensure_user(session, user_sub)
            stored = await session.get(UserLiteLLMKey, user_sub)
            if stored is None:
                session.add(UserLiteLLMKey(user_sub=user_sub, enc_value=encrypted))
            else:
                stored.enc_value = encrypted
            await session.commit()

    async def get_litellm_key(self, user_sub: str) -> str | None:
        async with self._session_factory() as session:
            stored = await session.get(UserLiteLLMKey, user_sub)
            if stored is None:
                return None
            return self._cipher.decrypt(stored.enc_value)

    async def upsert_secret(self, user_sub: str, mcp_name: str, header_name: str, value: str) -> None:
        encrypted = self._cipher.encrypt(value)
        normalized_mcp_name = mcp_name.strip()
        normalized_header = header_name.strip()
        async with self._session_factory() as session:
            await self._ensure_user(session, user_sub)
            stored = await session.scalar(
                select(UserSecret).where(
                    UserSecret.user_sub == user_sub,
                    UserSecret.mcp_name == normalized_mcp_name,
                    UserSecret.header_name == normalized_header,
                )
            )
            if stored is None:
                session.add(
                    UserSecret(
                        user_sub=user_sub,
                        mcp_name=normalized_mcp_name,
                        header_name=normalized_header,
                        enc_value=encrypted,
                    )
                )
            else:
                stored.enc_value = encrypted
            await session.commit()

    async def get_secrets(self, user_sub: str, mcp_name: str) -> dict[str, str]:
        normalized_mcp_name = mcp_name.strip()
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(UserSecret)
                    .where(
                        UserSecret.user_sub == user_sub,
                        UserSecret.mcp_name == normalized_mcp_name,
                    )
                    .order_by(UserSecret.header_name)
                )
            ).all()
            return {row.header_name: self._cipher.decrypt(row.enc_value) for row in rows}

    async def list_secret_headers(self, user_sub: str) -> dict[str, tuple[str, ...]]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(UserSecret)
                    .where(UserSecret.user_sub == user_sub)
                    .order_by(UserSecret.mcp_name, UserSecret.header_name)
                )
            ).all()

        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row.mcp_name, []).append(row.header_name)
        return {mcp_name: tuple(headers) for mcp_name, headers in grouped.items()}

    async def list_user_states(self) -> list[UserConfigurationState]:
        async with self._session_factory() as session:
            users = (await session.scalars(select(User).order_by(User.email, User.sub))).all()
            states: list[UserConfigurationState] = []
            for user in users:
                has_key = await session.get(UserLiteLLMKey, user.sub) is not None
                secret_count = len(
                    (
                        await session.scalars(
                            select(UserSecret).where(UserSecret.user_sub == user.sub)
                        )
                    ).all()
                )
                states.append(
                    UserConfigurationState(
                        sub=user.sub,
                        email=user.email,
                        has_litellm_key=has_key,
                        secret_count=secret_count,
                    )
                )
            return states

    async def _ensure_user(self, session: AsyncSession, sub: str) -> None:
        user = await session.get(User, sub)
        if user is None:
            session.add(User(sub=sub))
            await session.flush()
