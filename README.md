# mcp-broker

Self-hosted MCP proxy that lets standard Streamable HTTP MCP clients connect to LiteLLM MCP servers through Pocket ID OAuth, while injecting per-user LiteLLM keys and per-user, per-MCP secret headers toward LiteLLM.

This repository intentionally contains no runtime secrets. Configure deployments only with environment variables.

## Current Scope

- OAuth protected-resource metadata for standard MCP client discovery.
- Pocket ID JWT validation through JWKS.
- Per-user encrypted LiteLLM key and per-MCP secret header vault.
- Dynamic LiteLLM MCP discovery using admin catalog access plus user-key filtering.
- Admin-managed direct MCP catalog entries that can bypass LiteLLM when an upstream MCP server needs to own its OAuth flow.
- Streaming reverse proxy from `/{server}` to the LiteLLM `/{server}/mcp` endpoint with `Authorization` stripped and only user headers for that server injected.
- Delegated upstream OAuth passthrough for LiteLLM MCP servers that manage their own OAuth.
- In-memory per-user MCP request rate limit for mono-replica deployments.
- Server-rendered Jinja2 UI with HTMX only.

## Deployment

Build from the Dockerfile and expose port `8080` behind your HTTPS reverse proxy. Mount a persistent volume at `/data` for SQLite.

Copy `.env.example` into your deployment environment variables and fill values there. Do not commit real values.

Generate the Fernet key outside the repository:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

MCP client URLs are one per LiteLLM MCP server:

```text
https://your-broker-domain.example/deploy-tools
https://your-broker-domain.example/internal-tools
```

Admins can also add direct MCP entries from `/admin`. A direct entry still uses the broker URL publicly:

```text
https://your-broker-domain.example/external-workspace
```

but can proxy directly to an upstream endpoint such as:

```text
https://upstream-mcp.example.com/mcp
```

Use upstream OAuth passthrough for direct servers that run their own OAuth flow. In that mode the broker keeps the catalog entry and public URL, but OAuth endpoints and MCP traffic bypass LiteLLM.

`EXPECTED_AUDIENCE` accepts one or more comma-separated audiences. Include the audience that Pocket ID puts in access tokens for your MCP clients, for example a shared broker client ID and, if your IdP supports resource indicators, resource URLs:

```text
EXPECTED_AUDIENCE=broker-mcp-client,https://your-broker-domain.example/internal-tools
```

## Development

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
```

In this Codex sandbox, `aiosqlite` tests need to run outside the sandbox because the sandbox blocks `loop.call_soon_threadsafe`, which `aiosqlite` uses internally.
