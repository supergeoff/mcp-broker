# Direct MCP Catalog

Date: 2026-06-06

## Goal

Let broker admins add MCP servers to the public catalog that proxy directly to an MCP server URL instead of going through LiteLLM, while users keep seeing one complete catalog of broker-exposed MCP endpoints.

This supports servers such as `googlemcp`, where the upstream direct upstream OAuth proxy is designed to talk to the final MCP client directly and breaks when chained behind LiteLLM OAuth delegation.

## Scope

In scope:

- Keep all existing LiteLLM discovery and proxy behavior.
- Add admin-managed direct MCP catalog entries.
- Let each direct entry choose between broker-managed auth and upstream OAuth passthrough.
- Show direct entries in the same user dashboard catalog as LiteLLM entries.
- Proxy direct MCP traffic to the configured upstream URL.
- Proxy direct upstream OAuth metadata and OAuth endpoints for passthrough entries.
- Let admins delete direct entries.

Out of scope:

- Editing or deleting LiteLLM-discovered entries from the broker admin UI.
- Per-user authorization filtering for direct MCP entries.
- Validating upstream MCP protocol behavior when an admin saves a direct URL.
- Replacing LiteLLM discovery.
- Supporting stdio MCP servers.

## Catalog Model

The existing `mcp_servers` table becomes the single catalog table for both discovered LiteLLM servers and admin-added direct servers.

Each catalog row has:

- `name`: the public broker namespace, still validated by the existing MCP name rules.
- `source`: either `litellm` or `direct`.
- `direct_url`: required only when `source` is `direct`; it is the full upstream MCP endpoint such as `https://upstream-mcp.example.com/mcp`.
- `auth_mode`: either `broker` or `passthrough`; persisted through the existing `delegated_auth_passthrough` boolean.
- `required_headers_json`: unchanged; used for user secret-header forms and injection.
- `auth_type`: optional descriptive metadata such as `oauth2`.

The implementation adds `source` and `direct_url` columns, keeps the current `delegated_auth_passthrough` column, and treats `delegated_auth_passthrough = true` as `auth_mode = passthrough`. Existing rows default to `source = litellm`, `direct_url = null`, and keep their current delegated-auth behavior.

LiteLLM discovery upserts only LiteLLM-sourced rows. It must not overwrite direct rows with the same name. If a discovered LiteLLM server conflicts with an existing direct entry name, the direct entry wins and the LiteLLM row is skipped for that name.

## Admin Flow

The `/admin` page gains a "Direct MCP servers" section.

Admins can add a direct MCP entry with:

- Name, for example `googlemcp`.
- Direct MCP URL, for example `https://upstream-mcp.example.com/mcp`.
- Auth mode:
  - `passthrough`: use upstream OAuth directly.
  - `broker`: require Pocket ID at the broker before proxying to the direct URL.
- Optional auth type label.
- Optional required header names, entered as a comma-separated list and validated with the same secret-header rules already used elsewhere.

Admins can delete direct MCP entries. Deleting a direct entry removes only the catalog row. Existing user secrets for the same MCP name are not deleted by this feature and stop appearing in the dashboard once the catalog row is gone.

Non-admin users cannot add, edit, delete, or toggle catalog entries.

## User Catalog Flow

The dashboard continues to read `repository.list_mcp_servers()` and render one server list. Direct entries appear alongside LiteLLM entries with the same public broker URL shape:

```text
https://broker.example.com/googlemcp
```

The discovery button still refreshes LiteLLM-sourced metadata. It does not remove direct entries from the user-visible list.

For direct passthrough entries, the server card should indicate upstream OAuth passthrough. For direct broker-auth entries, the server card should indicate broker auth.

## Proxy Behavior

### LiteLLM Source

Existing behavior is preserved:

- Broker-auth LiteLLM entries proxy to `LITELLM_BASE_URL/{name}/mcp`.
- Passthrough LiteLLM entries proxy MCP traffic to `LITELLM_BASE_URL/{name}/mcp` and OAuth traffic to `LITELLM_BASE_URL/{name}/{endpoint}`.
- LiteLLM metadata is rewritten back to the broker public URL.

### Direct Source With Broker Auth

For `source = direct` and `auth_mode = broker`:

1. Client requests `/{name}`.
2. Broker requires a valid Pocket ID bearer token, as it does today for broker-managed LiteLLM servers.
3. Broker upserts the user and applies rate limiting.
4. Broker reads user secrets for that MCP name.
5. Broker proxies to the configured `direct_url`, preserving subpaths and query strings.
6. Broker strips the client `Authorization` header.
7. Broker injects only the user's saved secret headers for that MCP name.
8. Broker does not require or inject a LiteLLM key.

Subpaths append beneath the direct MCP URL. For example:

```text
/{name}/events -> {direct_url}/events
```

### Direct Source With Passthrough Auth

For `source = direct` and `auth_mode = passthrough`:

1. Client requests `/{name}` or an OAuth endpoint under `/{name}`.
2. Broker does not require Pocket ID.
3. Broker preserves the incoming `Authorization` header.
4. Broker strips hop-by-hop headers and broker-internal headers.
5. Broker proxies MCP traffic to the configured `direct_url`.
6. Broker proxies OAuth endpoints to sibling upstream endpoints derived from `direct_url`.

For a direct URL of:

```text
https://upstream-mcp.example.com/mcp
```

The OAuth endpoint mapping is:

```text
/googlemcp/authorize -> https://upstream-mcp.example.com/authorize
/googlemcp/token     -> https://upstream-mcp.example.com/token
/googlemcp/register  -> https://upstream-mcp.example.com/register
```

This lets direct upstream's OAuth proxy issue its consent and Google redirects against its own domain, while users still configure the broker URL as the MCP endpoint.

## OAuth Metadata

Broker-auth entries continue to publish broker protected-resource metadata pointing at Pocket ID.

Passthrough entries proxy upstream OAuth metadata. For direct entries, the upstream metadata URL is derived from the configured direct MCP URL:

```text
https://upstream-mcp.example.com/.well-known/oauth-protected-resource/mcp
https://upstream-mcp.example.com/.well-known/oauth-authorization-server/mcp
```

The broker rewrites upstream URLs so standard clients see broker URLs:

- Upstream resource `https://upstream-mcp.example.com/mcp` becomes `https://broker.example.com/googlemcp`.
- Upstream authorization server metadata URLs become `https://broker.example.com/.well-known/oauth-authorization-server/googlemcp`.
- Upstream OAuth endpoints under `https://upstream-mcp.example.com/authorize`, `/token`, and `/register` become broker endpoints under `/googlemcp/authorize`, `/googlemcp/token`, and `/googlemcp/register`.

If upstream metadata is unavailable or malformed, the broker returns the upstream status with a JSON body after applying the same safe response-header filtering used elsewhere.

## Error Handling

- Invalid direct MCP name: preserve existing 400 or 404 behavior.
- Direct URL missing for `source = direct`: reject admin form submission with 400.
- Direct URL not `http` or `https`: reject admin form submission with 400.
- Invalid required header name: reject admin form submission with 400.
- Unknown MCP name: preserve existing 401 challenge behavior for broker-auth requests when no catalog row exists; passthrough behavior only applies to known passthrough rows.
- Direct broker-auth request without user LiteLLM key: allowed; direct entries do not need LiteLLM keys.
- Direct broker-auth request without bearer token or with an invalid token: return the broker OAuth challenge.

## Testing

Use test-first implementation for behavior changes.

Planned tests:

- Storage persists and lists direct MCP configuration with source, direct URL, auth mode, auth type, and required headers.
- Storage keeps existing LiteLLM delegated-auth rows backwards-compatible.
- LiteLLM discovery does not overwrite an existing direct entry with the same name.
- Admin page renders direct MCP form and existing direct entries.
- Admin can add a direct passthrough entry.
- Admin can delete a direct entry.
- Non-admin users cannot add or delete direct entries.
- User dashboard renders direct entries in the same server list as LiteLLM entries.
- Direct broker-auth proxy strips client authorization, injects user secret headers, and does not require a LiteLLM key.
- Direct passthrough proxy preserves upstream authorization and does not require Pocket ID.
- Direct passthrough OAuth endpoints map from broker `/{name}/authorize`, `/token`, and `/register` to sibling upstream endpoints.
- Direct passthrough OAuth metadata is proxied and rewritten back to broker URLs.
- Full test suite passes.

## Acceptance Criteria

- Admins can add `googlemcp` as a direct MCP entry pointing to `https://upstream-mcp.example.com/mcp`.
- Admins can mark `googlemcp` as passthrough auth.
- Users see `googlemcp` in the same catalog as LiteLLM MCP servers.
- Users can configure clients with `https://broker.example.com/googlemcp`.
- `googlemcp` traffic no longer chains through LiteLLM.
- Existing LiteLLM MCP servers continue to work unchanged.
- Tests cover storage, admin UI, user catalog rendering, proxy routing, and OAuth metadata rewriting.
