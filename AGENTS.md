# Firekeep Agent Guide

Codex reads this file automatically for work in this repository.

## Repository expectations

- Firekeep is a multi-service repo. Before changing behavior, identify which service owns it: `cortex/`, `bridge/`, `sentinel/`, `relay/`, `symdex/` (client-side stdio code-intelligence package shipped via the client kit — not a server-side docker service), `dashboard/`, or a shared module such as `replay/`, `auth/`, `vault/`, `corpus/`.
- Prefer small, service-local changes. Do not refactor across multiple services unless the task actually requires shared contract changes.
- Treat docs, setup scripts, and dashboard references as part of the product surface. If you rename a tool, endpoint, env var, or setup path, update the docs in the same change.

## Consistency checklist

When you add, remove, or rename MCP tools, REST endpoints, env vars, setup behavior, or dashboard-visible features, check the matching surfaces:

- Service `app/mcp_server.py`
- Service API/router wiring
- `docker-compose.yml`
- `client/firekeep_client/cli.py` (installer) + `client/firekeep_client/adapters/*` (native-config render)
- `README.md`
- `CLAUDE.md`
- Relevant files in `docs/`
- `dashboard/` if the feature is user-visible there

## Validation

- For Python service changes, run the relevant service test suite before finishing.
- For script changes, run the script or a targeted smoke check when practical.
- For documentation-only changes, no test run is required, but keep examples and filenames consistent with the repository state.

## Codex setup

- Codex project instructions live in this `AGENTS.md`.
- MCP setup for Codex is documented in `docs/SETUP-CODEX.md`.
- Keep Codex guidance repository-scoped. Do not require contributors to copy project-specific instructions into `~/.codex/AGENTS.md`.
