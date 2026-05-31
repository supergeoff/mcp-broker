# mcp-broker

Self-hosted MCP proxy that lets `claude.ai` connect to LiteLLM MCP servers through Pocket ID OAuth, while injecting per-user LiteLLM keys and per-user `X-...` secrets toward LiteLLM.

This repository intentionally contains no runtime secrets. Configure deployments only with environment variables.

## Current Scope

- OAuth protected-resource metadata for claude.ai MCP discovery.
- Pocket ID JWT validation through JWKS.
- Per-user encrypted LiteLLM key and `X-...` header vault.
- Dynamic LiteLLM MCP discovery using admin catalog access plus user-key filtering.
- Streaming reverse proxy to `/mcp` and `/mcp/{server}` with `Authorization` stripped and user headers injected.
- In-memory per-user MCP request rate limit for mono-replica deployments.
- Server-rendered Jinja2 UI with HTMX only.

## Deployment

Build from the Dockerfile and expose port `8080` behind Dokploy HTTPS. Mount a persistent volume at `/data` for SQLite.

Copy `.env.example` into Dokploy environment variables and fill values there. Do not commit real values.

Generate the Fernet key outside the repository:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The claude.ai connector URL is:

```text
https://your-broker-domain.example/mcp
```

## Development

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
```

In this Codex sandbox, `aiosqlite` tests need to run outside the sandbox because the sandbox blocks `loop.call_soon_threadsafe`, which `aiosqlite` uses internally.
