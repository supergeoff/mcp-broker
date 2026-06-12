# Standard MCP Clients

Date: 2026-06-03

## Goal

Make mcp-broker work with any AI client that supports standard HTTP MCP authorization and Streamable HTTP, including standard MCP client and another standard MCP client.

The public client contract remains one simple URL per LiteLLM MCP server:

```text
https://broker.example.com/dokploy
https://broker.example.com/context7
```

This is standard-compatible because the MCP specification requires a single HTTP endpoint per MCP server; it does not require the endpoint path to be `/mcp`.

## Scope

Update the broker so the existing `/{mcp_name}` route is the only public MCP endpoint for each server, while removing client-specific assumptions from naming, documentation, and token validation.

In scope:

- Preserve `/{mcp_name}` as the Streamable HTTP MCP endpoint.
- Keep proxying upstream to LiteLLM `/{mcp_name}/mcp`.
- Keep Pocket ID as the OAuth authorization server for broker-managed MCP servers.
- Accept tokens for any configured audience that represents the broker MCP resource.
- Keep per-user LiteLLM key injection and per-user, per-MCP secret headers.
- Keep delegated-auth passthrough for upstream OAuth MCP servers.
- Update README, environment examples, UI copy, and tests so they refer to generic AI clients.

Out of scope:

- Adding a second `/mcp/{mcp_name}` route.
- Supporting stdio clients directly.
- Replacing Pocket ID or LiteLLM.
- Changing the vault data model.
- Adding OpenAPI or mcpo-specific behavior.

## Public MCP Contract

Each LiteLLM MCP server is exposed at one broker endpoint:

```text
/{mcp_name}
/{mcp_name}/{subpath}
```

The root route `/{mcp_name}` is the normal Streamable HTTP endpoint. It accepts the HTTP methods already supported by the broker and forwards the request to LiteLLM `/{mcp_name}/mcp`.

Subpaths remain supported because LiteLLM and some delegated OAuth flows use additional paths beneath the MCP server namespace. For example:

```text
/github/authorize -> /github/authorize
/github/token     -> /github/token
/context7/events  -> /context7/mcp/events
```

Reserved broker names remain blocked so MCP server names cannot shadow UI, auth, health, or API routes.

## OAuth Metadata And Challenges

For broker-managed MCP servers, the protected resource metadata remains:

```text
/.well-known/oauth-protected-resource/{mcp_name}
```

The metadata advertises:

```json
{
  "resource": "https://broker.example.com/dokploy",
  "authorization_servers": ["https://id.example.com"],
  "bearer_methods_supported": ["header"],
  "scopes_supported": ["openid", "email", "profile"],
  "resource_documentation": "https://broker.example.com/"
}
```

Unauthenticated or invalid-token MCP requests return a 401 challenge with:

```text
WWW-Authenticate: Bearer resource_metadata="https://broker.example.com/.well-known/oauth-protected-resource/dokploy"
```

This lets standard MCP clients discover Pocket ID without hardcoding client-specific behavior.

## JWT Audience

The current single `EXPECTED_AUDIENCE` setting is too client-specific. Replace its use with a normalized list of accepted audiences while preserving backwards compatibility for existing deployments.

Configuration behavior:

- `EXPECTED_AUDIENCE` remains supported.
- The value can be a comma-separated list.
- Each token is accepted if its `aud` claim matches any configured audience.

Deployments can therefore keep a client-specific audience during migration, or configure a broker resource audience shared by standard MCP clients.

## Broker-Managed MCP Flow

1. The AI client sends a request to `/{mcp_name}`.
2. If no valid bearer token is present, the broker returns the standard MCP OAuth challenge.
3. The client discovers Pocket ID from protected resource metadata and obtains an access token.
4. The broker validates issuer, audience, signature, expiry, subject, and token type.
5. The broker upserts the user record, applies the per-user rate limit, and reads that user's LiteLLM key.
6. The broker reads only that user's secrets for the requested MCP server.
7. The broker forwards to LiteLLM `/{mcp_name}/mcp`, stripping the client OAuth authorization header and injecting `x-litellm-api-key` plus scoped secret headers.

## Delegated Upstream OAuth Flow

For MCP servers marked `delegated_auth_passthrough`, the broker remains a transparent path-preserving proxy for upstream OAuth.

Metadata requests for delegated servers are proxied to LiteLLM legacy MCP OAuth metadata endpoints and rewritten back to the broker's public `/{mcp_name}` resource URL.

Delegated OAuth endpoints remain under the same public server namespace:

```text
/{mcp_name}/authorize
/{mcp_name}/token
/{mcp_name}/register
```

For delegated MCP requests, the broker preserves the incoming `Authorization` header and does not inject a LiteLLM user key.

## Error Handling

- Missing bearer token: return the standard 401 OAuth challenge.
- Invalid bearer token: return the same 401 OAuth challenge.
- Missing user LiteLLM key: return 412 with the dashboard URL.
- Unknown or invalid MCP server name: preserve existing 400 or 404 behavior.
- Rate limit exceeded: preserve existing 429 behavior.

## Testing

Use test-first implementation for behavior changes.

Planned tests:

- Metadata for `/{mcp_name}` advertises a generic protected resource, not a client-specific client.
- OAuth challenge points to the server-specific protected resource metadata.
- Proxying `/{mcp_name}` still forwards to LiteLLM `/{mcp_name}/mcp`.
- Subpaths still forward under LiteLLM `/{mcp_name}/mcp/{subpath}`.
- Delegated-auth metadata rewrites upstream URLs back to public `/{mcp_name}` URLs.
- JWT validation accepts any configured audience from a comma-separated list.
- JWT validation still rejects unconfigured audiences.
- README, `.env.example`, and UI copy are generic-client oriented.

## Acceptance Criteria

- A standard MCP Streamable HTTP client can be configured with `https://broker.example.com/{mcp_name}`.
- another standard MCP client can use the same per-server URL shape as standard MCP client.
- The broker has no public `/mcp/{mcp_name}` route.
- Existing per-user LiteLLM key and secret-header injection behavior is preserved.
- Existing delegated-auth passthrough behavior is preserved.
- Tests pass after the change.
