# Standard MCP Clients Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make mcp-broker work with standard MCP Streamable HTTP clients beyond standard MCP client while keeping the public endpoint shape `/{mcp_name}`.

**Architecture:** Keep FastAPI routing as-is for public MCP endpoints and update only client-specific assumptions. Parse `EXPECTED_AUDIENCE` as one or more accepted JWT audiences, pass that list to `JwtValidator`, and make docs/UI/tests describe generic AI clients.

**Tech Stack:** Python 3.14, FastAPI, Pydantic Settings, PyJWT, httpx, pytest, Jinja2.

---

## File Structure

- Modify `mcp_broker/config.py`: parse `EXPECTED_AUDIENCE` as a comma-separated list while keeping `ADMIN_EMAILS` parsing unchanged.
- Modify `mcp_broker/security.py`: accept one or more JWT audiences in `JwtValidator`.
- Modify `mcp_broker/app.py`: pass the accepted audience list to `JwtValidator`.
- Modify `tests/test_config.py`: cover comma-separated `EXPECTED_AUDIENCE`.
- Modify `tests/test_jwt_validator.py`: cover multiple accepted audiences and rejected unknown audiences.
- Modify `tests/conftest.py`: use a generic MCP audience in fixtures.
- Modify `tests/test_oauth_metadata.py`: rename client-specific test language.
- Modify `mcp_broker/templates/dashboard.html`: replace client-specific copy with generic AI-client copy.
- Modify `README.md`: document standard MCP clients and `/{mcp_name}` URLs.
- Modify `.env.example`: document comma-separated `EXPECTED_AUDIENCE`.

### Task 1: Multi-Audience Settings And JWT Validation

**Files:**
- Modify: `mcp_broker/config.py`
- Modify: `mcp_broker/security.py`
- Modify: `mcp_broker/app.py`
- Test: `tests/test_config.py`
- Test: `tests/test_jwt_validator.py`
- Test: `tests/conftest.py`

- [ ] **Step 1: Write failing config test**

```python
def test_settings_parses_expected_audience_list(monkeypatch, encryption_key: str) -> None:
    monkeypatch.setenv("PUBLIC_URL", "https://broker.example.com")
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", encryption_key)
    monkeypatch.setenv("SESSION_SECRET", "session-secret")
    monkeypatch.setenv("OIDC_ISSUER", "https://id.example.com")
    monkeypatch.setenv("UI_OIDC_CLIENT_ID", "ui-client")
    monkeypatch.setenv("UI_OIDC_CLIENT_SECRET", "ui-secret")
    monkeypatch.setenv("EXPECTED_AUDIENCE", "broker-mcp-client,https://broker.example.com/dokploy")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://litellm.example.com")
    monkeypatch.setenv("LITELLM_ADMIN_KEY", "litellm-admin")

    settings = Settings()

    assert settings.expected_audience == [
        "broker-mcp-client",
        "https://broker.example.com/dokploy",
    ]
```

- [ ] **Step 2: Run config test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_settings_parses_expected_audience_list -q`
Expected: FAIL because `settings.expected_audience` is still a raw string.

- [ ] **Step 3: Write failing JWT test**

```python
def test_jwt_validator_accepts_any_configured_audience() -> None:
    private_key, jwks = _keypair_and_jwks()
    validator = JwtValidator(
        issuer="https://id.example.com",
        audience=("broker-mcp-client", "https://broker.example.com/dokploy"),
        jwks=jwks,
    )

    claims = validator.verify(_token(private_key, aud="https://broker.example.com/dokploy"))

    assert claims["aud"] == "https://broker.example.com/dokploy"
```

- [ ] **Step 4: Run JWT test to verify it fails**

Run: `uv run pytest tests/test_jwt_validator.py::test_jwt_validator_accepts_any_configured_audience -q`
Expected: FAIL because `JwtValidator` currently stores one string audience.

- [ ] **Step 5: Implement config parsing**

Change `Settings.expected_audience` to a `list[str]` parsed from comma-separated env values:

```python
expected_audience: Annotated[list[str], NoDecode]

@field_validator("expected_audience", mode="before")
@classmethod
def parse_expected_audience(cls, value: object) -> list[str] | object:
    if isinstance(value, str):
        return [audience.strip() for audience in value.split(",") if audience.strip()]
    return value
```

- [ ] **Step 6: Implement JWT audience list support**

Use a tuple of accepted audiences in `JwtValidator`:

```python
from collections.abc import Sequence

def __init__(self, *, issuer: str, audience: str | Sequence[str], ...):
    self.audience = tuple(audience) if not isinstance(audience, str) else (audience,)
```

Keep `jwt.decode(..., audience=self.audience, ...)`.

- [ ] **Step 7: Run focused tests**

Run: `uv run pytest tests/test_config.py tests/test_jwt_validator.py -q`
Expected: all tests in those files pass.

### Task 2: Generic AI Client Copy And Documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `mcp_broker/templates/dashboard.html`
- Modify: `tests/conftest.py`
- Modify: `tests/test_oauth_metadata.py`
- Modify: `tests/test_proxy.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Update test and fixture wording**

Rename test functions and fixture values from client-specific names to generic MCP-client names:

```python
expected_audience="broker-mcp-client"
```

Use test names like:

```python
async def test_named_protected_resource_metadata_points_clients_to_pocket_id(...)
```

- [ ] **Step 2: Update dashboard copy**

Replace:

```html
Configure LiteLLM access and per-MCP secret headers for standard MCP clients.
```

With:

```html
Configure LiteLLM access and per-MCP secret headers for AI clients.
```

- [ ] **Step 3: Update README and env example**

Document the standard client URL shape:

```text
https://your-broker-domain.example/deploy-tools
https://your-broker-domain.example/internal-tools
```

Document `EXPECTED_AUDIENCE` as:

```text
EXPECTED_AUDIENCE=broker-mcp-client,https://your-broker-domain.example/deploy-tools
```

- [ ] **Step 4: Search for leftover client-specific references**

Run: `grep -R -n -i client-name README.md .env.example mcp_broker tests`
Expected: no product-copy references that imply the broker only supports standard MCP client. Test data can mention standard MCP client only when explicitly testing backwards-compatible audience migration.

- [ ] **Step 5: Run focused docs/copy-related tests**

Run: `uv run pytest tests/test_oauth_metadata.py tests/test_proxy.py tests/test_dashboard.py tests/test_config.py -q`
Expected: all focused tests pass.

### Task 3: Full Verification

**Files:**
- All modified files.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Inspect git status and diff**

Run: `git status --short`
Expected: modified files match the plan.

Run: `git diff --stat`
Expected: changes are limited to config, JWT validation, docs, UI copy, and tests.

- [ ] **Step 3: Commit implementation**

```bash
git add README.md .env.example mcp_broker/config.py mcp_broker/security.py mcp_broker/app.py mcp_broker/templates/dashboard.html tests
git commit -m "feat: support standard MCP clients"
```
