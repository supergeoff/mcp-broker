from dataclasses import dataclass, field
import json
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mcp_broker.models import McpServer, User, UserLiteLLMKey, UserSecret
from mcp_broker.security import FernetCipher


class Repository(Protocol):
    async def upsert_user(self, sub: str, email: str | None = None) -> None: ...
    async def get_litellm_key(self, user_sub: str) -> str | None: ...
    async def upsert_secret(self, user_sub: str, mcp_name: str, header_name: str, value: str) -> None: ...
    async def delete_secret(self, user_sub: str, mcp_name: str, header_name: str) -> None: ...
    async def delete_mcp_secrets(self, user_sub: str, mcp_name: str) -> None: ...
    async def get_secrets(self, user_sub: str, mcp_name: str) -> dict[str, str]: ...
    async def list_secret_headers(self, user_sub: str) -> dict[str, tuple[str, ...]]: ...
    async def upsert_mcp_servers(self, servers: list["McpServerConfiguration"]) -> None: ...
    async def upsert_direct_mcp_server(self, server: "McpServerConfiguration") -> None: ...
    async def delete_direct_mcp_server(self, mcp_name: str) -> None: ...
    async def delete_mcp_server(self, mcp_name: str) -> None: ...
    async def get_mcp_server(self, mcp_name: str) -> "McpServerConfiguration | None": ...
    async def list_mcp_servers(self) -> list["McpServerConfiguration"]: ...
    async def set_mcp_delegated_auth(self, mcp_name: str, enabled: bool) -> None: ...


@dataclass(frozen=True)
class UserConfigurationState:
    sub: str
    email: str | None
    has_litellm_key: bool
    secret_count: int


MCP_SOURCE_LITELLM = "litellm"
MCP_SOURCE_DIRECT = "direct"
MCP_SOURCES = {MCP_SOURCE_LITELLM, MCP_SOURCE_DIRECT}


@dataclass(frozen=True)
class McpServerConfiguration:
    name: str
    required_headers: tuple[str, ...]
    delegated_auth_passthrough: bool = False
    auth_type: str | None = None
    source: str = MCP_SOURCE_LITELLM
    direct_url: str | None = None
    static_headers: dict[str, str] = field(default_factory=dict)
    url_param_secrets: tuple[str, ...] = field(default_factory=tuple)
    active: bool = True


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

    async def delete_secret(self, user_sub: str, mcp_name: str, header_name: str) -> None:
        normalized_mcp_name = mcp_name.strip()
        normalized_header = header_name.strip()
        async with self._session_factory() as session:
            await session.execute(
                delete(UserSecret).where(
                    UserSecret.user_sub == user_sub,
                    UserSecret.mcp_name == normalized_mcp_name,
                    UserSecret.header_name == normalized_header,
                )
            )
            await session.commit()

    async def delete_mcp_secrets(self, user_sub: str, mcp_name: str) -> None:
        normalized_mcp_name = mcp_name.strip()
        async with self._session_factory() as session:
            await session.execute(
                delete(UserSecret).where(
                    UserSecret.user_sub == user_sub,
                    UserSecret.mcp_name == normalized_mcp_name,
                )
            )
            await session.commit()

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

    async def upsert_mcp_servers(self, servers: list[McpServerConfiguration]) -> None:
        async with self._session_factory() as session:
            active_names: set[str] = set()
            for server in servers:
                normalized = _normalize_mcp_server_configuration(
                    McpServerConfiguration(
                        name=server.name,
                        required_headers=server.required_headers,
                        delegated_auth_passthrough=server.delegated_auth_passthrough,
                        auth_type=server.auth_type,
                        source=MCP_SOURCE_LITELLM,
                        active=True,
                    )
                )
                active_names.add(normalized.name)
                stored = await session.get(McpServer, normalized.name)
                if stored is not None and stored.source == MCP_SOURCE_DIRECT:
                    continue
                required_headers_json = json.dumps(list(normalized.required_headers))
                if stored is None:
                    session.add(
                        McpServer(
                            name=normalized.name,
                            required_headers_json=required_headers_json,
                            delegated_auth_passthrough=normalized.delegated_auth_passthrough,
                            auth_type=normalized.auth_type,
                            source=MCP_SOURCE_LITELLM,
                            direct_url=None,
                            static_headers_json="{}",
                            url_param_secrets_json="[]",
                            active=True,
                        )
                    )
                else:
                    stored.required_headers_json = required_headers_json
                    stored.delegated_auth_passthrough = normalized.delegated_auth_passthrough
                    stored.auth_type = normalized.auth_type
                    stored.source = MCP_SOURCE_LITELLM
                    stored.direct_url = None
                    stored.static_headers_json = "{}"
                    stored.url_param_secrets_json = "[]"
                    stored.active = True
            rows = (
                await session.scalars(
                    select(McpServer).where(
                        McpServer.source == MCP_SOURCE_LITELLM,
                        McpServer.name.not_in(active_names),
                    )
                )
            ).all()
            for row in rows:
                row.active = False
            await session.commit()

    async def upsert_direct_mcp_server(self, server: McpServerConfiguration) -> None:
        normalized = _normalize_mcp_server_configuration(
            McpServerConfiguration(
                name=server.name,
                required_headers=server.required_headers,
                delegated_auth_passthrough=server.delegated_auth_passthrough,
                auth_type=server.auth_type,
                source=MCP_SOURCE_DIRECT,
                direct_url=server.direct_url,
                static_headers=server.static_headers,
                url_param_secrets=server.url_param_secrets,
            )
        )
        async with self._session_factory() as session:
            stored = await session.get(McpServer, normalized.name)
            required_headers_json = json.dumps(list(normalized.required_headers))
            static_headers_json = _encrypted_headers_json(normalized.static_headers, self._cipher)
            url_param_secrets_json = json.dumps(list(normalized.url_param_secrets))
            if stored is None:
                session.add(
                    McpServer(
                        name=normalized.name,
                        required_headers_json=required_headers_json,
                        delegated_auth_passthrough=normalized.delegated_auth_passthrough,
                        auth_type=normalized.auth_type,
                        source=normalized.source,
                        direct_url=normalized.direct_url,
                        static_headers_json=static_headers_json,
                        url_param_secrets_json=url_param_secrets_json,
                        active=True,
                    )
                )
            else:
                stored.required_headers_json = required_headers_json
                stored.delegated_auth_passthrough = normalized.delegated_auth_passthrough
                stored.auth_type = normalized.auth_type
                stored.source = normalized.source
                stored.direct_url = normalized.direct_url
                stored.static_headers_json = static_headers_json
                stored.url_param_secrets_json = url_param_secrets_json
                stored.active = True
            await session.commit()

    async def delete_direct_mcp_server(self, mcp_name: str) -> None:
        normalized_name = mcp_name.strip()
        async with self._session_factory() as session:
            stored = await session.get(McpServer, normalized_name)
            if stored is not None and stored.source == MCP_SOURCE_DIRECT:
                stored.active = False
            await session.commit()

    async def delete_mcp_server(self, mcp_name: str) -> None:
        normalized_name = mcp_name.strip()
        async with self._session_factory() as session:
            await session.execute(delete(McpServer).where(McpServer.name == normalized_name))
            await session.commit()

    async def get_mcp_server(self, mcp_name: str) -> McpServerConfiguration | None:
        async with self._session_factory() as session:
            stored = await session.get(McpServer, mcp_name.strip())
            if stored is None or not stored.active:
                return None
            return _mcp_server_configuration_from_row(stored, self._cipher)

    async def list_mcp_servers(self) -> list[McpServerConfiguration]:
        async with self._session_factory() as session:
            rows = (await session.scalars(select(McpServer).order_by(McpServer.name))).all()
            return [_mcp_server_configuration_from_row(row, self._cipher) for row in rows]

    async def set_mcp_delegated_auth(self, mcp_name: str, enabled: bool) -> None:
        normalized_name = mcp_name.strip()
        async with self._session_factory() as session:
            stored = await session.get(McpServer, normalized_name)
            if stored is None:
                session.add(
                    McpServer(
                        name=normalized_name,
                        required_headers_json="[]",
                        delegated_auth_passthrough=enabled,
                        source=MCP_SOURCE_LITELLM,
                        direct_url=None,
                        static_headers_json="{}",
                        url_param_secrets_json="[]",
                        active=True,
                    )
                )
            else:
                stored.delegated_auth_passthrough = enabled
            await session.commit()

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


def _normalize_mcp_server_configuration(server: McpServerConfiguration) -> McpServerConfiguration:
    source = server.source.strip().lower() if server.source else MCP_SOURCE_LITELLM
    if source not in MCP_SOURCES:
        raise ValueError("MCP server source must be litellm or direct")

    direct_url = server.direct_url.strip().rstrip("/") if server.direct_url else None
    if source == MCP_SOURCE_DIRECT and not direct_url:
        raise ValueError("Direct MCP servers require a direct_url")
    if source == MCP_SOURCE_LITELLM:
        direct_url = None
        static_headers: dict[str, str] = {}
        url_param_secrets: tuple[str, ...] = ()
    else:
        static_headers = {
            str(name).strip(): str(value)
            for name, value in server.static_headers.items()
            if str(name).strip()
        }
        url_param_secrets = tuple(sorted({s.strip() for s in server.url_param_secrets if s.strip()}))

    return McpServerConfiguration(
        name=server.name.strip(),
        required_headers=tuple(sorted({header.strip() for header in server.required_headers if header.strip()})),
        delegated_auth_passthrough=server.delegated_auth_passthrough,
        auth_type=server.auth_type.strip() if server.auth_type and server.auth_type.strip() else None,
        source=source,
        direct_url=direct_url,
        static_headers=dict(sorted(static_headers.items())),
        url_param_secrets=url_param_secrets,
        active=server.active,
    )


def _mcp_server_configuration_from_row(row: McpServer, cipher: FernetCipher) -> McpServerConfiguration:
    try:
        headers = json.loads(row.required_headers_json)
    except json.JSONDecodeError:
        headers = []
    try:
        url_param_secrets_raw = json.loads(row.url_param_secrets_json)
        if not isinstance(url_param_secrets_raw, list):
            url_param_secrets_raw = []
    except (json.JSONDecodeError, AttributeError):
        url_param_secrets_raw = []
    return McpServerConfiguration(
        name=row.name,
        required_headers=tuple(sorted(str(header) for header in headers if str(header).strip())),
        delegated_auth_passthrough=row.delegated_auth_passthrough,
        auth_type=row.auth_type,
        source=row.source or MCP_SOURCE_LITELLM,
        direct_url=row.direct_url,
        static_headers=_decrypted_headers_json(row.static_headers_json, cipher),
        url_param_secrets=tuple(sorted(str(s) for s in url_param_secrets_raw if str(s).strip())),
        active=row.active,
    )


def _encrypted_headers_json(headers: dict[str, str], cipher: FernetCipher) -> str:
    return json.dumps({name: cipher.encrypt(value) for name, value in headers.items()})


def _decrypted_headers_json(value: str | None, cipher: FernetCipher) -> dict[str, str]:
    if not value:
        return {}
    try:
        headers = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(headers, dict):
        return {}
    decrypted: dict[str, str] = {}
    for name, encrypted in headers.items():
        if not isinstance(name, str) or not isinstance(encrypted, str):
            continue
        decrypted[name] = cipher.decrypt(encrypted)
    return dict(sorted(decrypted.items()))
