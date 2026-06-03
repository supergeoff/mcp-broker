# mcp-broker

Self-hosted MCP proxy that lets standard AI clients such as Claude Code, Open WebUI, and other Streamable HTTP MCP clients connect to LiteLLM MCP servers through Pocket ID OAuth, while injecting per-user LiteLLM keys and per-user, per-MCP `X-...` secrets toward LiteLLM.

This repository intentionally contains no runtime secrets. Configure deployments only with environment variables.

## Current Scope

- OAuth protected-resource metadata for standard MCP client discovery.
- Pocket ID JWT validation through JWKS.
- Per-user encrypted LiteLLM key and per-MCP `X-...` header vault.
- Dynamic LiteLLM MCP discovery using admin catalog access plus user-key filtering.
- Streaming reverse proxy from `/{server}` to the LiteLLM `/{server}/mcp` endpoint with `Authorization` stripped and only user headers for that server injected.
- Delegated upstream OAuth passthrough for LiteLLM MCP servers that manage their own OAuth.
- In-memory per-user MCP request rate limit for mono-replica deployments.
- Server-rendered Jinja2 UI with HTMX only.

## Deployment

Build from the Dockerfile and expose port `8080` behind Dokploy HTTPS. Mount a persistent volume at `/data` for SQLite.

Copy `.env.example` into Dokploy environment variables and fill values there. Do not commit real values.

Generate the Fernet key outside the repository:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

MCP client URLs are one per LiteLLM MCP server:

```text
https://your-broker-domain.example/dokploy
https://your-broker-domain.example/context7
```

`EXPECTED_AUDIENCE` accepts one or more comma-separated audiences. Include the audience that Pocket ID puts in access tokens for your MCP clients, for example a shared broker client ID and, if your IdP supports resource indicators, resource URLs:

```text
EXPECTED_AUDIENCE=broker-mcp-client,https://your-broker-domain.example/dokploy
```

## Development

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
```

In this Codex sandbox, `aiosqlite` tests need to run outside the sandbox because the sandbox blocks `loop.call_soon_threadsafe`, which `aiosqlite` uses internally.
