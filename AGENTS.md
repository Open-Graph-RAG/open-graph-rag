# Repository Guidelines

## Project Structure & Module Organization

This repository runs LibreChat, LightRAG, n8n, MongoDB, and PostgreSQL through `compose.yaml`. The Python MCP bridge connects LibreChat to LightRAG’s document search API.

- `mcp/server.py`: authenticated, read-only knowledge search bridge.
- `mcp/tests/test_bridge.py`: protocol, authentication, and mocked upstream tests.
- `mcp/smoke.py`: live bridge smoke check; `mcp/Dockerfile`: bridge image.
- `librechat.yaml`: LibreChat configuration and MCP integration.
- `postgres/init.sql`: database initialization.
- `scripts/init_env.py`: initial environment and secret generation.
- `sample-data/`: demonstration documents for ingestion.

## Build, Test, and Development Commands

Run commands from the repository root:

```bash
python3 scripts/init_env.py                # Create .env once
# Fill in provider API keys in .env before starting services.
docker compose config --quiet             # Validate Compose configuration
docker compose up -d --build               # Build and start the stack
docker compose ps                         # Inspect service status
docker compose logs --tail=100 lightrag mcp librechat n8n
```

For bridge tests without Docker:

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.lock
.venv/bin/python -m unittest discover -s mcp/tests -v
```

Use `docker compose exec mcp python smoke.py` to check the running integration. This requires configured services and may invoke provider APIs.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` functions and variables, and uppercase configuration constants. Preserve type annotations and explicit argument validation in bridge tools. Use two-space indentation in YAML. Keep dependencies pinned and update the lock file when changing them. No formatter or linter is currently configured.

## Testing Guidelines

Tests use Python’s `unittest`, including `IsolatedAsyncioTestCase` and mocked HTTP requests. Name tests `test_*.py` and methods `test_*`. Cover changed authentication, input limits, source preservation, and upstream error handling. Unit tests must avoid paid API calls. No numerical coverage threshold is defined. For configuration changes, also validate Compose and document any live checks performed.

## Commit & Pull Request Guidelines

Existing commits use short descriptions such as `initial commit` and `remove env`; no formal commit convention is established. Use concise, imperative subjects. Pull requests should explain the change, link relevant issues, list validation commands and results, and identify configuration or dependency changes. Include screenshots when changing visible UI behavior.

## Security & Configuration Tips

Keep credentials in the ignored `.env`; document new settings in `.env.example`. Never commit secrets or database backups. Preserve bridge authentication and read-only tool behavior. `docker compose down` retains volumes; adding `-v` deletes persistent data.

## Autonomous Development Policy

Mission: develop Open Graph RAG as an open-source, self-hosted knowledge and decision layer for AI agents.

Rules for automated agents:
- Use one branch/worktree per task.
- Keep changes focused on the assigned scope.
- Add deterministic tests for behavior changes.
- Preserve MCP authentication and authorization.
- Do not change persistent storage contracts without an explicit migration plan.
- Do not modify production data or secrets.
- Never use `docker compose down -v`.
- Never use production backups in CI.
- Do not merge, release, or deploy autonomously.
- Do not treat agent review as human approval.

Definition of Done:
- Acceptance criteria satisfied.
- Relevant local tests pass.
- Required GitHub CI checks pass.
- Security and compatibility impact documented.
- Draft PR includes test evidence and risks.
