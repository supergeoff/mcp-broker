# Dokploy UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the mcp-broker Jinja UI into a Dokploy-like dashboard shell with automatic system light/dark theming.

**Architecture:** Keep the FastAPI/Jinja/HTMX stack. Add one shared Jinja shell template that owns the app layout and CSS tokens, then make dashboard and admin extend it while the discovery endpoint remains a partial inserted into the dashboard. Preserve the current named-MCP secret model already present in the worktree.

**Tech Stack:** FastAPI, Jinja2 templates, HTMX 2.0.7, pytest, httpx ASGI/MockTransport.

---

## File Structure

- Create `mcp_broker/templates/_shell.html`: shared HTML frame, sidebar navigation, Dokploy-like CSS variables, light/dark system theme, buttons, inputs, cards, badges, tables, and responsive rules.
- Modify `mcp_broker/templates/dashboard.html`: extend `_shell.html`, keep HTMX, render LiteLLM key card, MCP discovery card, and grouped saved headers/manual secret form.
- Modify `mcp_broker/templates/discover.html`: render discovered MCP servers as compact result rows/cards using the shell CSS classes.
- Modify `mcp_broker/templates/admin.html`: extend `_shell.html` and render the admin table with status badges.
- Modify `mcp_broker/app.py`: pass `current_page` and `is_admin` into dashboard/admin template contexts.
- Modify `tests/conftest.py`: add `FakeRepository.list_user_states()` for admin rendering tests.
- Modify `tests/test_dashboard.py`: add failing tests for shell/theme, admin table styling, and discovery partial styling.

The worktree already contains uncommitted changes in app, storage, proxy, models, templates, and tests. Do not revert them. Do not create implementation commits unless the user confirms how to handle the existing dirty worktree.

---

### Task 1: Add Rendering Tests First

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_dashboard.py`

- [ ] **Step 1: Add admin state support to the fake repository**

Append this import near the top of `tests/conftest.py`:

```python
from mcp_broker.storage import UserConfigurationState
```

Add this method inside `FakeRepository`:

```python
    async def list_user_states(self) -> list[UserConfigurationState]:
        return [
            UserConfigurationState(
                sub="pocket-sub",
                email="admin@example.com",
                has_litellm_key=self.litellm_key is not None,
                secret_count=sum(len(headers) for headers in self.secrets.values()),
            )
        ]
```

- [ ] **Step 2: Add dashboard shell/theme assertions**

Append this test to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 3: Add admin shell/table assertions**

Append this test to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 4: Add discovery partial assertions**

Append this test to `tests/test_dashboard.py`:

```python
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
```

- [ ] **Step 5: Run the focused tests and verify RED**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_dashboard.py -q
```

Expected result before implementation:

```text
FAILED tests/test_dashboard.py::test_dashboard_uses_dokploy_shell_and_system_theme
FAILED tests/test_dashboard.py::test_admin_uses_dokploy_shell_and_status_badges
FAILED tests/test_dashboard.py::test_discovery_partial_uses_result_cards_for_named_mcp
```

The existing dashboard save-secret tests should still pass.

---

### Task 2: Add Shared App Shell

**Files:**
- Create: `mcp_broker/templates/_shell.html`
- Modify: `mcp_broker/app.py`

- [ ] **Step 1: Create the shared shell template**

Create `mcp_broker/templates/_shell.html` with this structure:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% block title %}mcp-broker{% endblock %}</title>
    {% block head_extra %}{% endblock %}
    <style>
      :root {
        color-scheme: light dark;
        --background: #f5f5f5;
        --foreground: #0a0a0a;
        --card: #ffffff;
        --card-foreground: #0a0a0a;
        --muted: #f1f1f1;
        --muted-foreground: #737373;
        --border: #d7d7d7;
        --input: #e5e5e5;
        --primary: #171717;
        --primary-foreground: #fafafa;
        --accent: #ededed;
        --accent-foreground: #171717;
        --success: #047857;
        --success-surface: #d1fae5;
        --warning: #b45309;
        --warning-surface: #fef3c7;
        --radius: 10px;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }

      @media (prefers-color-scheme: dark) {
        :root {
          --background: #0d0d0d;
          --foreground: #fafafa;
          --card: #171717;
          --card-foreground: #fafafa;
          --muted: #212121;
          --muted-foreground: #b3b3b3;
          --border: #ffffff1a;
          --input: #ffffff26;
          --primary: #e5e5e5;
          --primary-foreground: #171717;
          --accent: #262626;
          --accent-foreground: #fafafa;
          --success: #34d399;
          --success-surface: #064e3b66;
          --warning: #fbbf24;
          --warning-surface: #78350f66;
        }
      }

      * { box-sizing: border-box; }
      body { margin: 0; background: var(--background); color: var(--foreground); font-size: 14px; letter-spacing: 0; }
      a { color: inherit; text-decoration: none; }
      button, input { font: inherit; }

      .app-shell { min-height: 100vh; display: grid; grid-template-columns: 268px minmax(0, 1fr); }
      .sidebar { display: flex; flex-direction: column; gap: 18px; padding: 18px; border-right: 1px solid var(--border); background: var(--background); }
      .brand { display: flex; align-items: center; gap: 10px; font-weight: 650; }
      .brand-mark { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--card); }
      .brand-subtitle, .muted { color: var(--muted-foreground); }
      .nav { display: grid; gap: 4px; }
      .nav-link { display: flex; align-items: center; gap: 10px; min-height: 38px; padding: 8px 10px; border-radius: var(--radius); color: var(--muted-foreground); transition: background .15s ease, color .15s ease; }
      .nav-link:hover, .nav-link[aria-current="page"] { background: var(--accent); color: var(--accent-foreground); }
      .nav-icon { width: 16px; height: 16px; flex: 0 0 16px; }
      .sidebar-footer { margin-top: auto; display: grid; gap: 10px; padding-top: 14px; border-top: 1px solid var(--border); }
      .user-email { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted-foreground); }
      .content { width: 100%; max-width: 1040px; padding: 34px 28px 48px; }
      .page-header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 22px; }
      .page-title { margin: 0; font-size: 28px; line-height: 1.15; font-weight: 650; }
      .page-description { margin: 8px 0 0; color: var(--muted-foreground); max-width: 620px; }
      .grid { display: grid; gap: 16px; }
      .two-column { grid-template-columns: minmax(0, 1.15fr) minmax(280px, .85fr); align-items: start; }
      .card { border: 1px solid var(--border); border-radius: calc(var(--radius) + 2px); background: var(--card); color: var(--card-foreground); padding: 18px; }
      .card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
      .card-title { margin: 0; font-size: 16px; font-weight: 650; }
      .card-description { margin: 6px 0 0; color: var(--muted-foreground); }
      .form-grid { display: grid; gap: 12px; }
      .row { display: grid; grid-template-columns: minmax(140px, .8fr) minmax(180px, 1fr) minmax(180px, 1.2fr) auto; gap: 10px; align-items: end; }
      .field { display: grid; gap: 7px; }
      label { color: var(--foreground); font-size: 13px; font-weight: 600; }
      input { width: 100%; min-height: 40px; border: 1px solid var(--input); border-radius: 8px; background: var(--card); color: var(--foreground); padding: 8px 10px; outline: none; }
      input:focus { border-color: var(--primary); box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 18%, transparent); }
      .actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
      .button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 40px; border: 1px solid transparent; border-radius: 8px; padding: 8px 13px; cursor: pointer; transition: background .15s ease, border-color .15s ease, color .15s ease, transform .1s ease; }
      .button:active { transform: scale(.98); }
      .button-primary { background: var(--primary); color: var(--primary-foreground); }
      .button-secondary { background: var(--card); color: var(--foreground); border-color: var(--border); }
      .button-secondary:hover, .button-ghost:hover { background: var(--accent); color: var(--accent-foreground); }
      .button-ghost { color: var(--muted-foreground); border-color: transparent; }
      .status-badge, .badge { display: inline-flex; align-items: center; gap: 6px; min-height: 24px; border-radius: 999px; padding: 3px 9px; font-size: 12px; font-weight: 600; white-space: nowrap; }
      .status-saved { background: var(--success-surface); color: var(--success); }
      .status-missing { background: var(--warning-surface); color: var(--warning); }
      .badge { border: 1px solid var(--border); background: var(--muted); color: var(--muted-foreground); }
      .badge-list { display: flex; flex-wrap: wrap; gap: 8px; }
      .saved-group { display: grid; gap: 8px; padding: 12px 0; border-top: 1px solid var(--border); }
      .saved-group:first-child { border-top: 0; padding-top: 0; }
      .saved-group-title { margin: 0; font-size: 14px; font-weight: 650; }
      .server-list { display: grid; gap: 12px; margin-top: 16px; }
      .server-card { border: 1px solid var(--border); border-radius: var(--radius); background: var(--background); padding: 14px; }
      .server-title { margin: 0 0 10px; font-size: 15px; font-weight: 650; }
      .empty-state { border: 1px dashed var(--border); border-radius: var(--radius); padding: 16px; color: var(--muted-foreground); background: var(--muted); }
      .table-card { overflow-x: auto; padding: 0; }
      .data-table { width: 100%; border-collapse: collapse; }
      .data-table th, .data-table td { padding: 13px 16px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
      .data-table th { color: var(--muted-foreground); font-size: 12px; font-weight: 650; text-transform: uppercase; }
      .data-table tr:last-child td { border-bottom: 0; }

      @media (max-width: 860px) {
        .app-shell { grid-template-columns: 1fr; }
        .sidebar { position: static; border-right: 0; border-bottom: 1px solid var(--border); }
        .nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .content { padding: 24px 18px 36px; }
        .page-header, .card-header { flex-direction: column; }
        .two-column, .row { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <div class="app-shell">
      <aside class="sidebar" aria-label="Primary navigation">
        <div>
          <a class="brand" href="/">
            <span class="brand-mark" aria-hidden="true">M</span>
            <span>
              <span>mcp-broker</span>
              <span class="brand-subtitle">OAuth MCP proxy</span>
            </span>
          </a>
        </div>
        <nav class="nav">
          <a class="nav-link" href="/" {% if current_page|default("dashboard") == "dashboard" %}aria-current="page"{% endif %}>
            <span class="nav-icon" aria-hidden="true">⌁</span>
            <span>Dashboard</span>
          </a>
          {% if is_admin|default(false) %}
            <a class="nav-link" href="/admin" {% if current_page|default("") == "admin" %}aria-current="page"{% endif %}>
              <span class="nav-icon" aria-hidden="true">▦</span>
              <span>Admin</span>
            </a>
          {% endif %}
        </nav>
        <div class="sidebar-footer">
          <div class="user-email">{{ user.email or user.sub }}</div>
          <a class="button button-ghost" href="/auth/logout">Logout</a>
        </div>
      </aside>
      <main class="content">
        {% block content %}{% endblock %}
      </main>
    </div>
  </body>
</html>
```

- [ ] **Step 2: Pass shell navigation context from FastAPI**

In `mcp_broker/app.py`, add an `is_admin` value in the dashboard context:

```python
        email = str(user.get("email") or "").lower()
        is_admin = email in settings.admin_emails
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "request": request,
                "user": user,
                "litellm_key_saved": litellm_key_saved,
                "secrets": secrets,
                "current_page": "dashboard",
                "is_admin": is_admin,
            },
        )
```

In the admin context, include:

```python
            context={
                "request": request,
                "user": user,
                "states": states,
                "current_page": "admin",
                "is_admin": True,
            },
```

- [ ] **Step 3: Run focused tests**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_dashboard.py -q
```

Expected: dashboard/admin tests may still fail until Task 3 replaces the templates.

---

### Task 3: Redesign Dashboard, Discovery, and Admin Templates

**Files:**
- Modify: `mcp_broker/templates/dashboard.html`
- Modify: `mcp_broker/templates/discover.html`
- Modify: `mcp_broker/templates/admin.html`

- [ ] **Step 1: Replace dashboard with shell-based content**

Replace `mcp_broker/templates/dashboard.html` with:

```html
{% extends "_shell.html" %}

{% block title %}mcp-broker{% endblock %}

{% block head_extra %}
  <script src="https://unpkg.com/htmx.org@2.0.7"></script>
{% endblock %}

{% block content %}
  <header class="page-header">
    <div>
      <h1 class="page-title">Dashboard</h1>
      <p class="page-description">Configure LiteLLM access and per-MCP secret headers for standard MCP clients.</p>
    </div>
    {% if litellm_key_saved %}
      <span class="status-badge status-saved">LiteLLM key saved</span>
    {% else %}
      <span class="status-badge status-missing">LiteLLM key missing</span>
    {% endif %}
  </header>

  <div class="grid two-column">
    <section class="card">
      <div class="card-header">
        <div>
          <h2 class="card-title">LiteLLM key</h2>
          <p class="card-description">Required before discovery and MCP proxying can work.</p>
        </div>
      </div>
      <form class="form-grid" method="post" action="/api/litellm-key">
        <div class="field">
          <label for="litellm_key">LiteLLM API key</label>
          <input id="litellm_key" name="litellm_key" type="password" autocomplete="off" required>
        </div>
        <div class="actions">
          <button class="button button-primary" type="submit">Save key</button>
        </div>
      </form>
    </section>

    <section class="card">
      <div class="card-header">
        <div>
          <h2 class="card-title">Saved headers</h2>
          <p class="card-description">Header names are visible; secret values stay hidden.</p>
        </div>
      </div>
      {% if secrets %}
        <div>
          {% for mcp_name, header_names in secrets.items() %}
            <div class="saved-group">
              <h3 class="saved-group-title">{{ mcp_name }}</h3>
              <div class="badge-list">
                {% for header_name in header_names %}
                  <span class="badge">{{ header_name }}</span>
                {% endfor %}
              </div>
            </div>
          {% endfor %}
        </div>
      {% else %}
        <div class="empty-state">No secret headers saved yet.</div>
      {% endif %}
    </section>
  </div>

  <section class="card" style="margin-top: 16px;">
    <div class="card-header">
      <div>
        <h2 class="card-title">MCP discovery</h2>
        <p class="card-description">Refresh accessible LiteLLM MCP servers and save required headers.</p>
      </div>
      <button class="button button-secondary" type="button" hx-post="/api/discover" hx-target="#discover-results" hx-swap="innerHTML">
        Refresh
      </button>
    </div>
    <div id="discover-results" class="server-list"></div>
  </section>

  <section class="card" style="margin-top: 16px;">
    <div class="card-header">
      <div>
        <h2 class="card-title">Add secret manually</h2>
        <p class="card-description">Use this when LiteLLM does not advertise header metadata.</p>
      </div>
    </div>
    <form class="form-grid" method="post" action="/api/secret">
      <div class="row">
        <div class="field">
          <label for="mcp_name">MCP name</label>
          <input id="mcp_name" name="mcp_name" autocomplete="off" placeholder="dokploy" required>
        </div>
        <div class="field">
          <label for="header_name">Header name</label>
          <input id="header_name" name="header_name" autocomplete="off" placeholder="X-DOKPLOY_API_KEY" required>
        </div>
        <div class="field">
          <label for="value">Secret value</label>
          <input id="value" name="value" type="password" autocomplete="off" required>
        </div>
        <button class="button button-primary" type="submit">Save</button>
      </div>
    </form>
  </section>
{% endblock %}
```

- [ ] **Step 2: Replace discovery partial with result cards**

Replace `mcp_broker/templates/discover.html` with:

```html
{% if servers %}
  {% for server in servers %}
    <div class="server-card">
      <h3 class="server-title">{{ server.name }}</h3>
      {% if server.required_headers %}
        <div class="form-grid">
          {% for header_name in server.required_headers %}
            <form class="row" method="post" action="/api/secret">
              <input type="hidden" name="mcp_name" value="{{ server.name }}">
              <input type="hidden" name="header_name" value="{{ header_name }}">
              <div class="field">
                <label>{{ header_name }}</label>
                <span class="{% if header_name in secrets.get(server.name, ()) %}status-badge status-saved{% else %}status-badge status-missing{% endif %}">
                  {% if header_name in secrets.get(server.name, ()) %}Saved{% else %}Required{% endif %}
                </span>
              </div>
              <div class="field">
                <label for="secret-{{ server.name }}-{{ loop.index }}">Secret value</label>
                <input id="secret-{{ server.name }}-{{ loop.index }}" name="value" type="password" autocomplete="off" required>
              </div>
              <button class="button button-primary" type="submit">Save</button>
            </form>
          {% endfor %}
        </div>
      {% else %}
        <div class="empty-state">No header metadata advertised.</div>
      {% endif %}
    </div>
  {% endfor %}
{% else %}
  <div class="empty-state">No accessible MCP server found for this key.</div>
{% endif %}
```

- [ ] **Step 3: Replace admin with shell-based content**

Replace `mcp_broker/templates/admin.html` with:

```html
{% extends "_shell.html" %}

{% block title %}mcp-broker admin{% endblock %}

{% block content %}
  <header class="page-header">
    <div>
      <h1 class="page-title">Admin</h1>
      <p class="page-description">Review user configuration state without exposing secret values.</p>
    </div>
  </header>

  <section class="card table-card">
    <table class="data-table">
      <thead>
        <tr>
          <th>User</th>
          <th>LiteLLM key</th>
          <th>Headers</th>
        </tr>
      </thead>
      <tbody>
        {% for state in states %}
          <tr>
            <td>{{ state.email or state.sub }}</td>
            <td>
              {% if state.has_litellm_key %}
                <span class="status-badge status-saved">saved</span>
              {% else %}
                <span class="status-badge status-missing">missing</span>
              {% endif %}
            </td>
            <td>{{ state.secret_count }}</td>
          </tr>
        {% else %}
          <tr>
            <td colspan="3">
              <div class="empty-state">No users found.</div>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </section>
{% endblock %}
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/test_dashboard.py -q
```

Expected:

```text
5 passed
```

If the exact count differs because more dashboard tests already exist, all tests in `tests/test_dashboard.py` must pass.

---

### Task 4: Full Verification and Cleanup

**Files:**
- Review: `mcp_broker/templates/_shell.html`
- Review: `mcp_broker/templates/dashboard.html`
- Review: `mcp_broker/templates/discover.html`
- Review: `mcp_broker/templates/admin.html`
- Review: `tests/test_dashboard.py`

- [ ] **Step 1: Scan for accidental secret exposure**

Run:

```bash
rg -n "enc_value|dokploy-user-token|litellm-user-key|value }}" mcp_broker/templates tests/test_dashboard.py
```

Expected: no template output displays secret values. Test fixtures may mention fake values only in test setup/assertions.

- [ ] **Step 2: Scan CSS for one-note palette or oversized hero styling**

Run:

```bash
rg -n "#[0-9a-fA-F]{3,8}|gradient|hero|orb|bokeh|letter-spacing" mcp_broker/templates/_shell.html
```

Expected:

```text
letter-spacing: 0
```

Hex colors should be neutral Dokploy/shadcn-like tokens with small success/warning accents. There must be no gradient/orb/hero decoration.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
```

Expected: all tests pass.

If sandbox restrictions affect `aiosqlite`, rerun with approval using the same command outside the sandbox. The README notes that `aiosqlite` tests can need this because the sandbox blocks `loop.call_soon_threadsafe`.

- [ ] **Step 4: Inspect final worktree without reverting user changes**

Run:

```bash
git --git-dir=.git-data --work-tree=. status --short
```

Expected: the implementation files are modified. Existing unrelated user changes may still appear; do not revert or commit them without explicit user direction.

---

## Self-Review

- Spec coverage: dashboard, discovery partial, admin page, theme tokens, system dark mode, responsive shell, compact Dokploy-like controls, no secret exposure, and tests are all covered.
- Placeholder scan: no open-ended marker words or omitted code blocks are intentionally used in this plan.
- Type consistency: `current_page`, `is_admin`, `UserConfigurationState`, `list_user_states`, `server-card`, `status-badge`, and `data-table` are named consistently across tests, templates, and app context.
