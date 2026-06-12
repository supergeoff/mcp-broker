# Direct MCP Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow admins to add direct MCP upstreams to the broker catalog, with either broker-managed auth or upstream OAuth passthrough, while users see one combined MCP catalog.

**Architecture:** Extend the existing `mcp_servers` catalog row with `source` and `direct_url`, keeping `delegated_auth_passthrough` as the persisted auth-mode flag. Route requests by catalog configuration: LiteLLM rows keep the current path logic, direct broker-auth rows proxy to their direct MCP URL after Pocket ID validation, and direct passthrough rows proxy MCP/OAuth/metadata directly to the upstream host.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy asyncio, httpx, Jinja2, pytest.

---

## File Structure

- Modify `mcp_broker/models.py`: add `source` and `direct_url` columns to `McpServer`.
- Modify `mcp_broker/storage.py`: add `source` and `direct_url` to `McpServerConfiguration`; add direct upsert/delete repository methods; skip LiteLLM discovery overwrites for direct rows.
- Modify `mcp_broker/app.py`: add startup schema migration, admin direct MCP endpoints, catalog routing by source/auth mode, direct metadata routing, and dashboard secret filtering.
- Modify `mcp_broker/proxy.py`: add direct MCP proxy helpers, direct OAuth endpoint mapping, direct OAuth metadata proxying, and direct metadata rewriting.
- Modify `mcp_broker/templates/admin.html`: render the direct MCP add form and existing direct entries.
- Modify `mcp_broker/templates/discover.html`: label `source` and auth mode clearly for user catalog cards.
- Modify `README.md`: document direct MCP catalog entries and `googlemcp` passthrough usage.
- Modify `tests/conftest.py`: update `FakeRepository` to implement direct catalog operations.
- Modify `tests/test_storage.py`: cover direct catalog persistence, conflict handling, and deletion.
- Modify `tests/test_dashboard.py`: cover admin UI/API, dashboard direct rendering, stale secret filtering, and non-admin protection.
- Modify `tests/test_proxy.py`: cover direct broker-auth proxying, direct passthrough proxying, and direct OAuth endpoints.
- Modify `tests/test_oauth_metadata.py`: cover direct passthrough OAuth metadata rewriting.

## Task 1: Catalog Model, Repository, And Schema Migration

**Files:**
- Modify: `mcp_broker/models.py`
- Modify: `mcp_broker/storage.py`
- Modify: `mcp_broker/app.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write failing storage test for direct server persistence**

Add this test to `tests/test_storage.py`:

```python
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
```

- [ ] **Step 2: Run the direct persistence test and verify it fails**

Run:

```bash
uv run pytest tests/test_storage.py::test_vault_repository_persists_direct_mcp_server_configuration -q
```

Expected: FAIL because `McpServerConfiguration` has no `source` or `direct_url` fields and the repository has no `upsert_direct_mcp_server`.

- [ ] **Step 3: Write failing storage test for LiteLLM/direct name conflicts**

Add this test to `tests/test_storage.py`:

```python
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
```

- [ ] **Step 4: Run the conflict test and verify it fails**

Run:

```bash
uv run pytest tests/test_storage.py::test_vault_repository_does_not_overwrite_direct_server_with_litellm_discovery -q
```

Expected: FAIL because the model and repository do not distinguish direct rows from LiteLLM rows.

- [ ] **Step 5: Write failing storage test for deleting only direct rows**

Add this test to `tests/test_storage.py`:

```python
async def test_vault_repository_deletes_only_direct_mcp_server(encryption_key) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = VaultRepository(session_factory, FernetCipher(encryption_key))

    await repository.upsert_mcp_servers(
        [
            McpServerConfiguration(
                name="context7",
                required_headers=(),
                delegated_auth_passthrough=True,
                auth_type="oauth2",
                source="litellm",
            )
        ]
    )
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

    await repository.delete_direct_mcp_server("context7")
    await repository.delete_direct_mcp_server("googlemcp")

    assert await repository.list_mcp_servers() == [
        McpServerConfiguration(
            name="context7",
            required_headers=(),
            delegated_auth_passthrough=True,
            auth_type="oauth2",
            source="litellm",
            direct_url=None,
        )
    ]

    await engine.dispose()
```

- [ ] **Step 6: Run the delete test and verify it fails**

Run:

```bash
uv run pytest tests/test_storage.py::test_vault_repository_deletes_only_direct_mcp_server -q
```

Expected: FAIL because `delete_direct_mcp_server` does not exist.

- [ ] **Step 7: Implement model columns**

In `mcp_broker/models.py`, update imports:

```python
from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
```

Keep `Text` already present and add these fields to `McpServer`:

```python
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="litellm")
    direct_url: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 8: Extend repository protocol and dataclass**

In `mcp_broker/storage.py`, extend the protocol:

```python
    async def upsert_direct_mcp_server(self, server: "McpServerConfiguration") -> None: ...
    async def delete_direct_mcp_server(self, mcp_name: str) -> None: ...
```

Replace `McpServerConfiguration` with:

```python
@dataclass(frozen=True)
class McpServerConfiguration:
    name: str
    required_headers: tuple[str, ...]
    delegated_auth_passthrough: bool = False
    auth_type: str | None = None
    source: str = "litellm"
    direct_url: str | None = None
```

Add constants near the dataclass:

```python
MCP_SOURCE_LITELLM = "litellm"
MCP_SOURCE_DIRECT = "direct"
MCP_SOURCES = {MCP_SOURCE_LITELLM, MCP_SOURCE_DIRECT}
```

- [ ] **Step 9: Implement direct upsert/delete and conflict protection**

In `VaultRepository`, add:

```python
    async def upsert_direct_mcp_server(self, server: McpServerConfiguration) -> None:
        normalized = _normalize_mcp_server_configuration(
            McpServerConfiguration(
                name=server.name,
                required_headers=server.required_headers,
                delegated_auth_passthrough=server.delegated_auth_passthrough,
                auth_type=server.auth_type,
                source=MCP_SOURCE_DIRECT,
                direct_url=server.direct_url,
            )
        )
        async with self._session_factory() as session:
            stored = await session.get(McpServer, normalized.name)
            required_headers_json = json.dumps(list(normalized.required_headers))
            if stored is None:
                session.add(
                    McpServer(
                        name=normalized.name,
                        required_headers_json=required_headers_json,
                        delegated_auth_passthrough=normalized.delegated_auth_passthrough,
                        auth_type=normalized.auth_type,
                        source=normalized.source,
                        direct_url=normalized.direct_url,
                    )
                )
            else:
                stored.required_headers_json = required_headers_json
                stored.delegated_auth_passthrough = normalized.delegated_auth_passthrough
                stored.auth_type = normalized.auth_type
                stored.source = normalized.source
                stored.direct_url = normalized.direct_url
            await session.commit()

    async def delete_direct_mcp_server(self, mcp_name: str) -> None:
        normalized_name = mcp_name.strip()
        async with self._session_factory() as session:
            await session.execute(
                delete(McpServer).where(
                    McpServer.name == normalized_name,
                    McpServer.source == MCP_SOURCE_DIRECT,
                )
            )
            await session.commit()
```

Update `upsert_mcp_servers` so LiteLLM discovery rows cannot overwrite direct rows:

```python
                if stored is not None and stored.source == MCP_SOURCE_DIRECT:
                    continue
```

When creating or updating LiteLLM rows, set:

```python
source=MCP_SOURCE_LITELLM
direct_url=None
```

and:

```python
                    stored.source = MCP_SOURCE_LITELLM
                    stored.direct_url = None
```

- [ ] **Step 10: Implement normalization and row conversion**

Replace `_normalize_mcp_server_configuration` with:

```python
def _normalize_mcp_server_configuration(server: McpServerConfiguration) -> McpServerConfiguration:
    source = server.source.strip().lower() if server.source else MCP_SOURCE_LITELLM
    if source not in MCP_SOURCES:
        raise ValueError("MCP server source must be litellm or direct")

    direct_url = server.direct_url.strip().rstrip("/") if server.direct_url else None
    if source == MCP_SOURCE_DIRECT and not direct_url:
        raise ValueError("Direct MCP servers require a direct_url")
    if source == MCP_SOURCE_LITELLM:
        direct_url = None

    return McpServerConfiguration(
        name=server.name.strip(),
        required_headers=tuple(sorted({header.strip() for header in server.required_headers if header.strip()})),
        delegated_auth_passthrough=server.delegated_auth_passthrough,
        auth_type=server.auth_type.strip() if server.auth_type and server.auth_type.strip() else None,
        source=source,
        direct_url=direct_url,
    )
```

Replace `_mcp_server_configuration_from_row` return with:

```python
    return McpServerConfiguration(
        name=row.name,
        required_headers=tuple(sorted(str(header) for header in headers if str(header).strip())),
        delegated_auth_passthrough=row.delegated_auth_passthrough,
        auth_type=row.auth_type,
        source=row.source or MCP_SOURCE_LITELLM,
        direct_url=row.direct_url,
    )
```

- [ ] **Step 11: Add startup schema migration for existing deployments**

In `mcp_broker/app.py`, add imports:

```python
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
```

Add this helper near the bottom of the file:

```python
def _ensure_mcp_server_catalog_columns(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("mcp_servers")}
    if "source" not in columns:
        connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'litellm'"))
    if "direct_url" not in columns:
        connection.execute(text("ALTER TABLE mcp_servers ADD COLUMN direct_url TEXT"))
```

In lifespan after `Base.metadata.create_all`, run:

```python
                await connection.run_sync(_ensure_mcp_server_catalog_columns)
```

- [ ] **Step 12: Update fake repository**

In `tests/conftest.py`, update `FakeRepository.upsert_mcp_servers` to preserve direct conflicts:

```python
    async def upsert_mcp_servers(self, servers: list[McpServerConfiguration]) -> None:
        for server in servers:
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
            )
```

Add:

```python
    async def upsert_direct_mcp_server(self, server: McpServerConfiguration) -> None:
        self.mcp_servers[server.name] = McpServerConfiguration(
            name=server.name,
            required_headers=tuple(sorted(server.required_headers)),
            delegated_auth_passthrough=server.delegated_auth_passthrough,
            auth_type=server.auth_type,
            source="direct",
            direct_url=server.direct_url.rstrip("/") if server.direct_url else None,
        )

    async def delete_direct_mcp_server(self, mcp_name: str) -> None:
        existing = self.mcp_servers.get(mcp_name)
        if existing is not None and existing.source == "direct":
            self.mcp_servers.pop(mcp_name, None)
```

Update `set_mcp_delegated_auth` to preserve `source` and `direct_url`:

```python
            source=existing.source if existing else "litellm",
            direct_url=existing.direct_url if existing else None,
```

- [ ] **Step 13: Run focused storage tests**

Run:

```bash
uv run pytest tests/test_storage.py tests/test_discovery.py -q
```

Expected: PASS.

- [ ] **Step 14: Commit catalog model and storage**

Run:

```bash
git add mcp_broker/models.py mcp_broker/storage.py mcp_broker/app.py tests/conftest.py tests/test_storage.py
git commit -m "feat: store direct MCP catalog entries"
```

## Task 2: Admin Direct MCP UI And API

**Files:**
- Modify: `mcp_broker/app.py`
- Modify: `mcp_broker/templates/admin.html`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing admin render test**

Add this test to `tests/test_dashboard.py`:

```python
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
    assert "googlemcp" in response.text
    assert "https://googlemcp.example.com/mcp" in response.text
    assert 'action="/api/mcp/direct/delete"' in response.text
```

- [ ] **Step 2: Run admin render test and verify it fails**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_admin_renders_direct_mcp_form_and_entries -q
```

Expected: FAIL because the admin template does not render direct MCP controls.

- [ ] **Step 3: Write failing admin add test**

Add this test to `tests/test_dashboard.py`:

```python
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
                "required_headers": "X-GOOGLE-WORKSPACE, X-GOOGLE-ORG",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert fake_repository.mcp_servers["googlemcp"] == McpServerConfiguration(
        name="googlemcp",
        required_headers=("X-GOOGLE-ORG", "X-GOOGLE-WORKSPACE"),
        delegated_auth_passthrough=True,
        auth_type="oauth2",
        source="direct",
        direct_url="https://googlemcp.example.com/mcp",
    )
```

- [ ] **Step 4: Run admin add test and verify it fails**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_admin_adds_direct_passthrough_mcp -q
```

Expected: FAIL because `/api/mcp/direct` does not exist.

- [ ] **Step 5: Write failing admin delete test**

Add this test to `tests/test_dashboard.py`:

```python
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
    assert fake_repository.mcp_servers == {}
```

- [ ] **Step 6: Run admin delete test and verify it fails**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_admin_deletes_direct_mcp -q
```

Expected: FAIL because `/api/mcp/direct/delete` does not exist.

- [ ] **Step 7: Write failing non-admin protection test**

Add this test to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 8: Run non-admin test and verify it fails**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_non_admin_cannot_add_or_delete_direct_mcp -q
```

Expected: FAIL because the routes do not exist.

- [ ] **Step 9: Add admin route context**

In `mcp_broker/app.py`, update `/admin` to fetch direct servers:

```python
        repository = _repository(app)
        states = await repository.list_user_states()
        direct_servers = [
            server for server in await repository.list_mcp_servers() if server.source == "direct"
        ]
```

Pass:

```python
                "direct_servers": direct_servers,
```

- [ ] **Step 10: Add direct MCP admin endpoints**

In `mcp_broker/app.py`, add imports:

```python
from urllib.parse import urlparse
```

Add routes before `/admin` or immediately after it:

```python
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
        auth_type = str(form.get("auth_type", "")).strip() or None
        await _repository(app).upsert_direct_mcp_server(
            McpServerConfiguration(
                name=mcp_name,
                required_headers=required_headers,
                delegated_auth_passthrough=auth_mode == "passthrough",
                auth_type=auth_type,
                source="direct",
                direct_url=direct_url,
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
```

Add helpers:

```python
def _normalize_direct_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Direct MCP URL must be an absolute http or https URL")
    if parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="Direct MCP URL must not include query strings or fragments")
    return value.rstrip("/")


def _parse_required_headers(value: str) -> tuple[str, ...]:
    headers = tuple(
        normalize_secret_header_name(header)
        for header in value.split(",")
        if normalize_secret_header_name(header)
    )
    invalid = [header for header in headers if not is_valid_secret_header_name(header)]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid required header: {invalid[0]}")
    return tuple(sorted(set(headers)))
```

- [ ] **Step 11: Render direct MCP admin controls**

In `mcp_broker/templates/admin.html`, after the user table section, add:

```html
  <section class="card" style="margin-top: 16px;">
    <div class="card-header">
      <div>
        <h2 class="card-title">Direct MCP servers</h2>
        <p class="card-description">Add MCP endpoints that should bypass LiteLLM.</p>
      </div>
    </div>
    <form class="form-grid" method="post" action="/api/mcp/direct">
      <div class="field">
        <label for="direct-mcp-name">Name</label>
        <input id="direct-mcp-name" name="name" autocomplete="off" placeholder="googlemcp" required>
      </div>
      <div class="field">
        <label for="direct-mcp-url">Direct MCP URL</label>
        <input id="direct-mcp-url" name="direct_url" autocomplete="off" placeholder="https://upstream-mcp.example.com/mcp" required>
      </div>
      <div class="field">
        <label for="direct-mcp-auth-mode">Auth mode</label>
        <select id="direct-mcp-auth-mode" name="auth_mode">
          <option value="passthrough">Upstream OAuth passthrough</option>
          <option value="broker">Broker auth</option>
        </select>
      </div>
      <div class="field">
        <label for="direct-mcp-auth-type">Auth type</label>
        <input id="direct-mcp-auth-type" name="auth_type" autocomplete="off" placeholder="oauth2">
      </div>
      <div class="field">
        <label for="direct-mcp-headers">Required headers</label>
        <input id="direct-mcp-headers" name="required_headers" autocomplete="off" placeholder="X-API-KEY, X-ORG-ID">
      </div>
      <div class="actions">
        <button class="button button-primary" type="submit">Add direct MCP</button>
      </div>
    </form>

    {% if direct_servers %}
      <div class="server-list" style="margin-top: 16px;">
        {% for server in direct_servers %}
          <div class="server-card">
            <div class="server-card-header">
              <div>
                <h3 class="server-title">{{ server.name }}</h3>
                <div class="badge-list">
                  <span class="badge">Direct</span>
                  {% if server.auth_type %}
                    <span class="badge">{{ server.auth_type }}</span>
                  {% endif %}
                  {% if server.delegated_auth_passthrough %}
                    <span class="status-badge status-saved">Upstream OAuth passthrough</span>
                  {% else %}
                    <span class="status-badge status-missing">Broker auth</span>
                  {% endif %}
                </div>
                <p class="card-description">{{ server.direct_url }}</p>
              </div>
              <form method="post" action="/api/mcp/direct/delete">
                <input type="hidden" name="name" value="{{ server.name }}">
                <button class="button button-danger button-small" type="submit">Delete</button>
              </form>
            </div>
          </div>
        {% endfor %}
      </div>
    {% else %}
      <div class="empty-state" style="margin-top: 16px;">No direct MCP servers configured.</div>
    {% endif %}
  </section>
```

- [ ] **Step 12: Add select styling**

In `mcp_broker/templates/_shell.html`, update the font selector:

```css
      button, input, select { font: inherit; }
```

Replace the existing `input { ... }` selector with:

```css
      input,
      select {
        width: 100%;
        min-height: 40px;
        border: 1px solid var(--input);
        border-radius: 8px;
        background: var(--card);
        color: var(--foreground);
        padding: 8px 10px;
        outline: none;
      }
```

Replace the existing `input:focus { ... }` selector with:

```css
      input:focus,
      select:focus {
        border-color: var(--primary);
        box-shadow: 0 0 0 2px rgba(23, 23, 23, .18);
      }
```

- [ ] **Step 13: Run focused admin tests**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_admin_renders_direct_mcp_form_and_entries tests/test_dashboard.py::test_admin_adds_direct_passthrough_mcp tests/test_dashboard.py::test_admin_deletes_direct_mcp tests/test_dashboard.py::test_non_admin_cannot_add_or_delete_direct_mcp -q
```

Expected: PASS.

- [ ] **Step 14: Commit admin direct MCP controls**

Run:

```bash
git add mcp_broker/app.py mcp_broker/templates/admin.html mcp_broker/templates/_shell.html tests/test_dashboard.py
git commit -m "feat: add direct MCP admin controls"
```

## Task 3: User Catalog Rendering And Secret Filtering

**Files:**
- Modify: `mcp_broker/app.py`
- Modify: `mcp_broker/templates/discover.html`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Write failing dashboard direct rendering test**

Add this test to `tests/test_dashboard.py`:

```python
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
    assert "Upstream OAuth passthrough" in response.text
```

- [ ] **Step 2: Run dashboard direct rendering test and verify it fails**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_renders_direct_mcp_in_catalog -q
```

Expected: FAIL because direct source labels are not rendered.

- [ ] **Step 3: Write failing stale secret filtering test**

Add this test to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 4: Run stale secret test and verify it fails**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_hides_saved_headers_for_servers_not_in_catalog -q
```

Expected: FAIL because dashboard currently renders all saved secret groups.

- [ ] **Step 5: Filter dashboard secrets by current catalog**

In `mcp_broker/app.py`, update dashboard:

```python
        mcp_servers = await repository.list_mcp_servers()
        catalog_names = {server.name for server in mcp_servers}
        secrets = {
            mcp_name: header_names
            for mcp_name, header_names in (await repository.list_secret_headers(user["sub"])).items()
            if mcp_name in catalog_names
        }
```

Make sure `mcp_servers` is loaded before this filter and remove the old unfiltered `secrets` assignment.

- [ ] **Step 6: Label source and auth mode in discovery partial**

In `mcp_broker/templates/discover.html`, add source badges in the `badge-list`:

```html
            {% if server.source == "direct" %}
              <span class="badge">Direct</span>
            {% else %}
              <span class="badge">LiteLLM</span>
            {% endif %}
```

Replace the passthrough/broker labels with:

```html
            {% if server.delegated_auth_passthrough %}
              <span class="status-badge status-saved">Upstream OAuth passthrough</span>
            {% else %}
              <span class="status-badge status-missing">Broker auth</span>
            {% endif %}
```

- [ ] **Step 7: Run focused dashboard tests**

Run:

```bash
uv run pytest tests/test_dashboard.py::test_dashboard_renders_direct_mcp_in_catalog tests/test_dashboard.py::test_dashboard_hides_saved_headers_for_servers_not_in_catalog tests/test_dashboard.py::test_dashboard_renders_discovered_mcp_server_boxes_from_storage -q
```

Expected: PASS.

- [ ] **Step 8: Commit catalog rendering**

Run:

```bash
git add mcp_broker/app.py mcp_broker/templates/discover.html tests/test_dashboard.py
git commit -m "feat: show direct MCP entries in user catalog"
```

## Task 4: Direct MCP Proxy Routing

**Files:**
- Modify: `mcp_broker/app.py`
- Modify: `mcp_broker/proxy.py`
- Test: `tests/test_proxy.py`

- [ ] **Step 1: Write failing direct broker-auth proxy test**

Add this test to `tests/test_proxy.py`:

```python
async def test_direct_broker_auth_mcp_proxies_without_litellm_key(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(200, content=b"direct-ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            litellm_key=None,
            secrets={"googlemcp": {"X-GOOGLE-WORKSPACE": "workspace-token"}},
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=("X-GOOGLE-WORKSPACE",),
                    delegated_auth_passthrough=False,
                    auth_type=None,
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            },
        ),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/googlemcp?stream=true",
            headers={"Authorization": "Bearer oauth-access-token"},
            content=b'{"jsonrpc":"2.0"}',
        )

    assert response.status_code == 200
    assert response.text == "direct-ok"
    assert captured["url"] == "https://googlemcp.example.com/mcp?stream=true"
    assert captured["body"] == b'{"jsonrpc":"2.0"}'
    assert captured["headers"]["x-google-workspace"] == "workspace-token"
    assert "authorization" not in captured["headers"]
    assert "x-litellm-api-key" not in captured["headers"]
```

- [ ] **Step 2: Run direct broker-auth test and verify it fails**

Run:

```bash
uv run pytest tests/test_proxy.py::test_direct_broker_auth_mcp_proxies_without_litellm_key -q
```

Expected: FAIL because direct broker-auth rows still follow LiteLLM proxy logic and require a LiteLLM key.

- [ ] **Step 3: Write failing direct passthrough MCP proxy test**

Add this test to `tests/test_proxy.py`:

```python
async def test_direct_passthrough_mcp_preserves_authorization_without_pocket_id(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.aread()
        return httpx.Response(200, content=b"direct-passthrough-ok")

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        jwt_validator=FakeJwtValidator(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/googlemcp/events?cursor=abc",
            headers={"Authorization": "Bearer upstream-token"},
            content=b"{}",
        )

    assert response.status_code == 200
    assert response.text == "direct-passthrough-ok"
    assert captured["url"] == "https://googlemcp.example.com/mcp/events?cursor=abc"
    assert captured["body"] == b"{}"
    assert captured["headers"]["authorization"] == "Bearer upstream-token"
    assert "x-litellm-api-key" not in captured["headers"]
```

- [ ] **Step 4: Run direct passthrough MCP test and verify it fails**

Run:

```bash
uv run pytest tests/test_proxy.py::test_direct_passthrough_mcp_preserves_authorization_without_pocket_id -q
```

Expected: FAIL because passthrough direct rows still proxy through LiteLLM.

- [ ] **Step 5: Write failing direct OAuth endpoint mapping test**

Add this test to `tests/test_proxy.py`:

```python
async def test_direct_passthrough_oauth_endpoints_map_to_upstream_siblings(settings) -> None:
    captured: list[tuple[str, str, str, bytes, dict[str, str]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                request.method,
                request.url.path,
                request.url.query.decode(),
                await request.aread(),
                dict(request.headers),
            )
        )
        if request.url.path == "/authorize":
            return httpx.Response(302, headers={"location": "https://accounts.google.com/o/oauth2/v2/auth"})
        return httpx.Response(200, json={"access_token": "upstream-token"})

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        authorize_response = await client.get("/googlemcp/authorize?client_id=standard-mcp-client")
        token_response = await client.post(
            "/googlemcp/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            content=b"grant_type=authorization_code&code=abc",
        )

    assert authorize_response.status_code == 302
    assert authorize_response.headers["location"] == "https://accounts.google.com/o/oauth2/v2/auth"
    assert token_response.status_code == 200
    assert captured[0][0] == "GET"
    assert captured[0][1] == "/authorize"
    assert captured[0][2] == "client_id=standard-mcp-client"
    assert captured[1][0] == "POST"
    assert captured[1][1] == "/token"
    assert captured[1][3] == b"grant_type=authorization_code&code=abc"
    assert "x-litellm-api-key" not in captured[0][4]
    assert "x-litellm-api-key" not in captured[1][4]
```

- [ ] **Step 6: Run direct OAuth endpoint test and verify it fails**

Run:

```bash
uv run pytest tests/test_proxy.py::test_direct_passthrough_oauth_endpoints_map_to_upstream_siblings -q
```

Expected: FAIL because direct OAuth endpoint mapping does not exist.

- [ ] **Step 7: Implement direct proxy helpers**

In `mcp_broker/proxy.py`, import `McpServerConfiguration`:

```python
from mcp_broker.storage import McpServerConfiguration, Repository
```

Add direct proxy functions:

```python
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
    upstream_request = http_client.build_request(
        request.method,
        _direct_mcp_url(server.direct_url, subpath, request.url.query),
        headers=_direct_broker_upstream_headers(request.headers, secrets),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_direct_passthrough_mcp_request(
    *,
    request: Request,
    server: McpServerConfiguration,
    subpath: str,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    upstream_request = http_client.build_request(
        request.method,
        _direct_mcp_url(server.direct_url, subpath, request.url.query),
        headers=_delegated_upstream_headers(request.headers),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )


async def proxy_direct_oauth_endpoint_request(
    *,
    request: Request,
    server: McpServerConfiguration,
    endpoint: str,
    http_client: httpx.AsyncClient,
) -> StreamingResponse:
    if not server.direct_url:
        raise HTTPException(status_code=500, detail="Direct MCP server is missing direct_url")
    upstream_request = http_client.build_request(
        request.method,
        _direct_oauth_url(server.direct_url, endpoint, request.url.query),
        headers=_delegated_upstream_headers(request.headers),
        content=await request.body(),
    )
    upstream_response = await http_client.send(upstream_request, stream=True)
    return StreamingResponse(
        _response_body(upstream_response),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
        background=BackgroundTask(upstream_response.aclose),
    )
```

Add helpers:

```python
def _direct_broker_upstream_headers(
    incoming: Mapping[str, str],
    secrets: Mapping[str, str],
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming.items()
        if name.lower() not in REQUEST_BLOCKLIST
    }
    headers.update(secrets)
    return headers


def _direct_mcp_url(direct_url: str, subpath: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    path = url.path.rstrip("/") or "/"
    if subpath:
        path = f"{path}/{subpath.strip('/')}"
    return url.copy_with(path=path, query=query.encode("utf-8"))


def _direct_oauth_url(direct_url: str, endpoint: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    parent = url.path.rstrip("/").rsplit("/", 1)[0]
    path = f"{parent}/{endpoint}" if parent else f"/{endpoint}"
    return url.copy_with(path=path, query=query.encode("utf-8"))
```

- [ ] **Step 8: Route direct servers in the app**

In `mcp_broker/app.py`, update imports from `mcp_broker.proxy`:

```python
from mcp_broker.proxy import proxy_delegated_litellm_request, proxy_delegated_mcp_request
from mcp_broker.proxy import proxy_delegated_oauth_metadata_request, proxy_mcp_request
from mcp_broker.proxy import proxy_direct_broker_mcp_request, proxy_direct_oauth_endpoint_request
from mcp_broker.proxy import proxy_direct_passthrough_mcp_request
```

In `_handle_mcp`, load the server first:

```python
        server = await _repository(app).get_mcp_server(mcp_name)
        if server and server.delegated_auth_passthrough:
            if server.source == "direct":
                return await proxy_direct_passthrough_mcp_request(
                    request=request,
                    server=server,
                    subpath=subpath,
                    http_client=_http_client(app),
                )
            return await proxy_delegated_mcp_request(
                request=request,
                mcp_name=mcp_name,
                subpath=subpath,
                settings=settings,
                http_client=_http_client(app),
            )
```

After JWT validation, route direct broker-auth rows before `proxy_mcp_request`:

```python
        if server and server.source == "direct":
            return await proxy_direct_broker_mcp_request(
                request=request,
                server=server,
                subpath=subpath,
                user_sub=claims["sub"],
                repository=_repository(app),
                http_client=_http_client(app),
            )
```

In `_handle_delegated_oauth_endpoint`, load the server:

```python
        server = await _repository(app).get_mcp_server(mcp_name)
        if not server or not server.delegated_auth_passthrough:
            return await _handle_mcp(request, mcp_name, endpoint)
        if server.source == "direct":
            return await proxy_direct_oauth_endpoint_request(
                request=request,
                server=server,
                endpoint=endpoint,
                http_client=_http_client(app),
            )
        return await proxy_delegated_litellm_request(
            request=request,
            path=f"/{mcp_name}/{endpoint}",
            settings=settings,
            http_client=_http_client(app),
        )
```

- [ ] **Step 9: Remove or simplify `_is_delegated_mcp` usage**

Replace `_is_delegated_mcp` callers with direct server checks from Step 8. Keep `_is_delegated_mcp` only if one remaining metadata branch still uses it; otherwise remove it.

- [ ] **Step 10: Run focused direct proxy tests**

Run:

```bash
uv run pytest tests/test_proxy.py::test_direct_broker_auth_mcp_proxies_without_litellm_key tests/test_proxy.py::test_direct_passthrough_mcp_preserves_authorization_without_pocket_id tests/test_proxy.py::test_direct_passthrough_oauth_endpoints_map_to_upstream_siblings -q
```

Expected: PASS.

- [ ] **Step 11: Run existing proxy regression tests**

Run:

```bash
uv run pytest tests/test_proxy.py -q
```

Expected: PASS.

- [ ] **Step 12: Commit direct proxy routing**

Run:

```bash
git add mcp_broker/app.py mcp_broker/proxy.py tests/test_proxy.py
git commit -m "feat: proxy direct MCP upstreams"
```

## Task 5: Direct OAuth Metadata Passthrough

**Files:**
- Modify: `mcp_broker/app.py`
- Modify: `mcp_broker/proxy.py`
- Test: `tests/test_oauth_metadata.py`

- [ ] **Step 1: Write failing direct protected-resource metadata test**

Add imports to `tests/test_oauth_metadata.py`:

```python
from mcp_broker.storage import McpServerConfiguration
from tests.conftest import FakeRepository
```

Add this test:

```python
async def test_direct_passthrough_protected_resource_metadata_is_proxied_and_rewritten(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "resource": "https://googlemcp.example.com/mcp",
                "authorization_servers": [
                    "https://googlemcp.example.com/.well-known/oauth-authorization-server/mcp"
                ],
            },
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource/googlemcp")

    assert response.status_code == 200
    assert captured["path"] == "/.well-known/oauth-protected-resource/mcp"
    assert response.json() == {
        "resource": "https://broker.example.com/googlemcp",
        "authorization_servers": [
            "https://broker.example.com/.well-known/oauth-authorization-server/googlemcp"
        ],
    }
```

- [ ] **Step 2: Run direct protected-resource metadata test and verify it fails**

Run:

```bash
uv run pytest tests/test_oauth_metadata.py::test_direct_passthrough_protected_resource_metadata_is_proxied_and_rewritten -q
```

Expected: FAIL because direct metadata passthrough does not exist.

- [ ] **Step 3: Write failing direct authorization-server metadata test**

Add this test to `tests/test_oauth_metadata.py`:

```python
async def test_direct_passthrough_authorization_server_metadata_is_proxied_and_rewritten(settings) -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "issuer": "https://googlemcp.example.com",
                "authorization_endpoint": "https://googlemcp.example.com/authorize",
                "token_endpoint": "https://googlemcp.example.com/token",
                "registration_endpoint": "https://googlemcp.example.com/register",
            },
        )

    app = create_app(
        settings=settings,
        repository=FakeRepository(
            mcp_servers={
                "googlemcp": McpServerConfiguration(
                    name="googlemcp",
                    required_headers=(),
                    delegated_auth_passthrough=True,
                    auth_type="oauth2",
                    source="direct",
                    direct_url="https://googlemcp.example.com/mcp",
                )
            }
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/.well-known/oauth-authorization-server/googlemcp")

    assert response.status_code == 200
    assert captured["path"] == "/.well-known/oauth-authorization-server/mcp"
    assert response.json() == {
        "issuer": "https://googlemcp.example.com",
        "authorization_endpoint": "https://broker.example.com/googlemcp/authorize",
        "token_endpoint": "https://broker.example.com/googlemcp/token",
        "registration_endpoint": "https://broker.example.com/googlemcp/register",
    }
```

- [ ] **Step 4: Run direct authorization-server metadata test and verify it fails**

Run:

```bash
uv run pytest tests/test_oauth_metadata.py::test_direct_passthrough_authorization_server_metadata_is_proxied_and_rewritten -q
```

Expected: FAIL because direct auth-server metadata passthrough does not exist.

- [ ] **Step 5: Add direct metadata proxy helper**

In `mcp_broker/proxy.py`, add:

```python
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
    upstream_url = _direct_metadata_url(server.direct_url, metadata_kind, request.url.query)
    upstream_response = await http_client.get(upstream_url, headers=_delegated_upstream_headers(request.headers))
    try:
        payload = upstream_response.json()
    except ValueError:
        payload = {}
    return JSONResponse(
        _rewrite_direct_metadata(payload, settings, server),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers),
    )
```

Add helpers:

```python
def _direct_metadata_url(direct_url: str, metadata_kind: str, query: str) -> httpx.URL:
    url = httpx.URL(direct_url)
    resource_path = url.path.strip("/")
    path = f"/.well-known/{metadata_kind}"
    if resource_path:
        path = f"{path}/{resource_path}"
    return url.copy_with(path=path, query=query.encode("utf-8"))


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
    direct_url = str(httpx.URL(server.direct_url)).rstrip("/")
    rewritten = value.replace(direct_url, public_mcp_url)

    for metadata_kind, public_kind in (
        ("oauth-protected-resource", "oauth-protected-resource"),
        ("oauth-authorization-server", "oauth-authorization-server"),
    ):
        upstream = str(_direct_metadata_url(server.direct_url, metadata_kind, "")).rstrip("?")
        public = f"{settings.public_url}/.well-known/{public_kind}/{server.name}"
        rewritten = rewritten.replace(upstream, public)

    for endpoint in ("authorize", "token", "register"):
        upstream = str(_direct_oauth_url(server.direct_url, endpoint, "")).rstrip("?")
        rewritten = rewritten.replace(upstream, f"{public_mcp_url}/{endpoint}")
    return rewritten
```

- [ ] **Step 6: Route direct metadata in app**

In `mcp_broker/app.py`, import:

```python
from mcp_broker.proxy import proxy_direct_oauth_metadata_request
```

In `named_protected_resource_metadata`, replace the delegated check with:

```python
        server = await _repository(app).get_mcp_server(normalized_mcp_name)
        if server and server.delegated_auth_passthrough:
            if server.source == "direct":
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
```

In `named_authorization_server_metadata`, replace the delegated check with:

```python
        server = await _repository(app).get_mcp_server(normalized_mcp_name)
        if not server or not server.delegated_auth_passthrough:
            raise HTTPException(status_code=404, detail="MCP server not found")
        if server.source == "direct":
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
```

- [ ] **Step 7: Run focused metadata tests**

Run:

```bash
uv run pytest tests/test_oauth_metadata.py::test_direct_passthrough_protected_resource_metadata_is_proxied_and_rewritten tests/test_oauth_metadata.py::test_direct_passthrough_authorization_server_metadata_is_proxied_and_rewritten -q
```

Expected: PASS.

- [ ] **Step 8: Run metadata regression tests**

Run:

```bash
uv run pytest tests/test_oauth_metadata.py tests/test_proxy.py::test_delegated_auth_metadata_is_proxied_to_litellm_legacy_mcp_oauth_endpoint -q
```

Expected: PASS.

- [ ] **Step 9: Commit direct OAuth metadata**

Run:

```bash
git add mcp_broker/app.py mcp_broker/proxy.py tests/test_oauth_metadata.py
git commit -m "feat: proxy direct MCP OAuth metadata"
```

## Task 6: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Verify: all modified files.

- [ ] **Step 1: Update README**

In `README.md`, update the scope list with:

```markdown
- Admin-managed direct MCP catalog entries that can bypass LiteLLM when an upstream MCP server needs to own its OAuth flow.
```

After the MCP client URL examples, add:

```markdown
Admins can also add direct MCP entries from `/admin`. A direct entry still uses the broker URL publicly:

```text
https://your-broker-domain.example/external-workspace
```

but can proxy directly to an upstream endpoint such as:

```text
https://upstream-mcp.example.com/mcp
```

Use upstream OAuth passthrough for direct servers that run their own OAuth proxy, such as direct upstream OAuth servers. In that mode the broker keeps the catalog entry and public URL, but OAuth endpoints and MCP traffic bypass LiteLLM.
```

- [ ] **Step 2: Run all tests**

Run:

```bash
uv run pytest -q
```

Expected: PASS.

If `aiosqlite` fails in the Codex sandbox with `loop.call_soon_threadsafe`, rerun the same command with sandbox escalation because this repository documents that behavior in `README.md`.

- [ ] **Step 3: Inspect git diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: changes are limited to the files named in this plan.

- [ ] **Step 4: Commit docs and final verification**

Run:

```bash
git add README.md
git commit -m "docs: document direct MCP catalog entries"
```

- [ ] **Step 5: Final smoke command**

Run:

```bash
uv run pytest tests/test_storage.py tests/test_dashboard.py tests/test_proxy.py tests/test_oauth_metadata.py -q
```

Expected: PASS.

## Self-Review

- Spec coverage: catalog model, direct admin add/delete, combined user catalog, direct broker-auth proxying, direct passthrough proxying, OAuth metadata rewriting, error handling, and tests are all covered by tasks.
- Placeholder scan: no task uses open-ended implementation placeholders; each behavior has concrete tests, code targets, commands, and expected outcomes.
- Type consistency: `source`, `direct_url`, `delegated_auth_passthrough`, `McpServerConfiguration`, `upsert_direct_mcp_server`, and `delete_direct_mcp_server` are named consistently across storage, app, templates, fake repository, and tests.
