# mcp-broker Dokploy UI Redesign

Date: 2026-06-02

## Goal

Redesign the server-rendered mcp-broker UI so it feels like a small Dokploy-adjacent operations dashboard. The UI must support both light and dark appearances and follow the user's system theme automatically.

The redesign keeps the current FastAPI, Jinja, and HTMX architecture. It does not introduce a frontend build step.

## Reference

The visual direction is based on the public Dokploy login page and Dokploy documentation:

- Compact Inter/system typography.
- Neutral light and dark surfaces.
- Rounded 8px to 12px controls and cards.
- Fine borders, muted secondary text, and restrained hover states.
- Sidebar/dashboard structure for operational tools.
- Theme tokens similar to `background`, `foreground`, `card`, `muted`, `border`, `input`, `primary`, and `accent`.
- Dark mode driven by the system preference.

## Scope

Update these UI surfaces:

- `/` dashboard.
- `/api/discover` partial rendered into the dashboard.
- `/admin` user state table.

The implementation may add a shared base template or shared stylesheet if that keeps the templates cleaner. It may add small context values such as `is_admin` to support navigation state.

## Non Goals

- No React, Tailwind build, or JavaScript framework.
- No route, auth, storage, or proxy behavior changes.
- No new persistence behavior.
- No manual theme toggle unless it is already trivial and does not distract from system theme support.

## Layout

Use an app shell:

- Left sidebar on desktop with brand, short subtitle, navigation, and user identity.
- Responsive top/header layout on mobile.
- Main content max width around 1040px with comfortable vertical spacing.
- Cards for individual settings and workflows, not nested cards.

Dashboard content:

- Top title row with "Dashboard", a short description, and LiteLLM key status.
- LiteLLM key card with password input and primary save button.
- MCP discovery card with HTMX refresh button and a results area.
- Saved headers card with compact badges or an empty state.

Discovery partial:

- Empty state when no accessible MCP server is found.
- Per-server rows/cards with server name and required header forms.
- Required headers use compact password inputs and save buttons.
- Already saved headers show a saved hint without exposing values.

Admin content:

- Same app shell.
- Admin title and description.
- Table with user, LiteLLM key status, and header count.
- Status badges for saved/missing.

## Theme

Use CSS custom properties in the templates or a shared stylesheet:

- Light defaults approximate Dokploy/shadcn neutral tokens.
- Dark values are provided inside `@media (prefers-color-scheme: dark)`.
- `color-scheme: light dark` is set so native controls follow the mode.
- Avoid a one-color palette. Use neutral surfaces with small semantic accents for success and warning.

Representative tokens:

- `--background`, `--foreground`
- `--card`, `--card-foreground`
- `--muted`, `--muted-foreground`
- `--border`, `--input`
- `--primary`, `--primary-foreground`
- `--accent`, `--accent-foreground`
- `--success`, `--warning`

## Components

Create local CSS classes for:

- App shell and sidebar.
- Page header.
- Cards and card headers.
- Buttons: primary, secondary, ghost.
- Inputs and labels.
- Badges and status pills.
- Tables.
- Empty states.
- HTMX loading state if simple enough.

Use inline SVG icons only where they materially improve scanning and do not add noise. Keep icon dimensions fixed.

## Error Handling

Existing backend errors can remain unchanged. The UI should visually handle:

- Missing LiteLLM key before discovery.
- No discovered servers.
- No saved headers.
- Admin table with zero rows.

## Testing

Use test-first implementation where behavior changes are needed.

Planned tests:

- Dashboard still renders for an authenticated user.
- Dashboard includes the new shell/landmarks and system-theme CSS.
- Admin renders the redesigned table for an admin user.
- Discovery partial renders server/header forms with the new classes.

Run the project test suite after changes. If sandbox restrictions affect SQLite or aiosqlite, rerun the relevant command with approval as documented in the README.

## Acceptance Criteria

- The UI has Dokploy-like density, neutral theme tokens, sidebar structure, and compact controls.
- Light and dark modes both work through `prefers-color-scheme`.
- Dashboard, discovery partial, and admin page are responsive.
- Existing forms and HTMX behavior still work.
- No secret values are displayed.
- Tests pass or any environment-related test limitation is clearly reported.
