# Ontology UI — integration contract

Read this before implementing. Both the designer and fixer lanes read this
file as the single source of truth. Do not invent extra routes, template
names, or context variables; if something is missing, flag it instead of
adding silently.

## Stack

- FastAPI (Jinja2Templates), opaque server-side sessions.
- HTMX 1.9.10 served from `/static/htmx.min.js` (no CDN; offline-capable).
- No JS framework. Vanilla JS only for the optional theme toggle and
  HTMX hooks. No build step.
- Read-only: zero write/POST endpoints beyond `/login` and `/logout`.

## Service identity

- Container name: `ontology-ui`
- Port: 8020 default (`${ONTOLOGY_UI_PORT:-8020}` exposed on
  `127.0.0.1` only). The host port is intentionally distinct from the
  ontology API (`ONTOLOGY_PORT`, default 8010) so the two services can
  coexist on the same host. The in-container port stays `8010`.
- New env vars (add to `.env.example`):
  - `ONTOLOGY_UI_PORT=8020`
  - `ONTOLOGY_UI_HTTPS_ONLY=1` when served behind HTTPS.
  - `ONTOLOGY_API_URL=http://ontology:8010` (internal docker DNS).
- Depends on `ontology` (service_healthy).

## Auth model

- The UI never accepts or stores a static API key. The user logs in by
  pasting their **ontology bearer token** plus the **workspace name** into
  `/login`. The API validates the token and workspace grant before the UI
  creates an opaque, random, server-side session; browser cookies never contain credentials.
  The UI forwards them on every upstream request as
  `Authorization: Bearer <token>` and `X-Workspace: <workspace>`.
- If the session is missing/expired or upstream returns 401, the UI revokes
  and clears it, then redirects to `/login` with an expiry notice. A 403 on
  an established session renders the error panel and does not revoke it;
  workspace configuration should be corrected by the operator.
- `/logout` (POST) revokes the session and clears its cookie.
- Sessions expire after eight hours and are held in a bounded (10,000-session)
  in-memory store. Deploy exactly one UI process/replica; multi-worker or
  multi-replica deployment is unsupported unless a shared session backend is added.
- Cookies are HttpOnly, SameSite=Strict, and Secure when `ONTOLOGY_UI_HTTPS_ONLY=1`.
- The token is **never rendered** in any page after login. The UI may
  display the last 4 characters of the token as a friendly handle in
  the top bar, and nothing else.
- Login's workspace input is not an authorization source: the ontology API
  validates the bearer token against the configured workspace and token grants.

## Routes (UI server)

| Method | Path                                                         | Auth      | Template                       | Context keys                                                                                              |
|--------|--------------------------------------------------------------|-----------|--------------------------------|-----------------------------------------------------------------------------------------------------------|
| GET    | `/`                                                          | session   | `dashboard.html`               | `health`, `ontology_count`, `active_versions_count`, `fact_count`, `outbox_pending`, `outbox_failed`, `last_audit_at`, `now` |
| GET    | `/ontologies`                                                | session   | `ontologies.html`              | `ontologies: list[dict]` (id, active_version, latest_version, latest_status, created_at, updated_at)      |
| GET    | `/ontologies/{ontology_id}/versions`                         | session   | `ontology_versions.html`       | `ontology_id`, `versions: list[dict]` (version, status, content_hash, created_by, created_at, published_by, published_at) |
| GET    | `/ontologies/{ontology_id}/versions/{version}`               | session   | `ontology_version.html`        | `ontology_id`, `version`, `definition: dict` (id, version, status, entities, relations, validation)         |
| GET    | `/ontologies/{ontology_id}/versions/{version}/extraction-profile` | session | `extraction_profile.html`   | `ontology_id`, `version`, `profile: dict` (ontology_id, ontology_version, profile, governance)             |
| GET    | `/facts`                                                     | session   | `facts.html`                   | `facts: list[dict]`, `filters: dict` (kind, ontology_id, predicate, q), `counts: dict`                     |
| GET    | `/quarantine`                                                | session   | `quarantine.html`              | `items: list[dict]` (id, ontology_id, record, errors, mode, created_by, created_at)                      |
| GET    | `/projection-status`                                         | session   | `projection.html`              | `rows: list[dict]` (id, fact_id, attempts, available_at, leased_until, delivered_at, last_error)           |
| GET    | `/audit`                                                     | session   | `audit.html`                   | `entries: list[dict]` (sequence, actor, action, subject, details, created_at)                               |
| GET    | `/health`                                                    | public    | —                              | Returns `{"status": "ok"}` if the ontology upstream is reachable                                         |
| GET    | `/login`                                                     | public    | `login.html`                   | `next: str`, `error: str|None`, `workspace_hint: str`                                                      |
| POST   | `/login`                                                     | public    | redirect                       | Form fields: `token` (min 32), `workspace`, `next`. API authorizes the token/workspace before issuing the session cookie. |
| POST   | `/logout`                                                    | public    | redirect                       | Clears session, redirects to `/login`.                                                                    |

The `dashboard.html` overview must be the homepage. It shows four KPIs
and short tables. Health failures must not 500 the page; render `—`
and a banner with a "Retry" link.

### Pagination

Server-side pagination on `/facts`, `/quarantine`, `/projection-status`,
`/audit` using `?page=N&page_size=M`. Default `page=1`, `page_size=50`,
`page_size` capped at 200. `pagination` partial expects `page`,
`page_size`, `total`, `path`, `query_args` (current query minus
`page`).

### Error mapping (UI → user)

- 401 from upstream → clear session, redirect to `/login` with
  `?next=<original-path>` and flash "Session expired or token invalid."
- 403 → render error template, do not clear session (workspace mismatch
  is a config issue).
- 404 → render "Not found" panel with ontology/fact id and a "Back"
  link.
- 502/503 → render "Upstream unavailable" panel; do not cache.
- All other 4xx/5xx → render the upstream `detail` field as a
  formatted code block.

## Templates directory

`services/ontology_ui/templates/`

```
base.html                     # layout, nav, login gate, flash messages
login.html                    # token + workspace form
_partials/
  error_banner.html           # flashed errors (red panel)
  status_pill.html            # status: published | draft | quarantined | observed | accepted | failed | pending
  pagination.html             # prev/next + jump
  property_table.html         # entity property table
  relation_table.html         # relation source/target/directed table
  json_view.html              # <pre><code> JSON pretty-print with copy
  facts_table.html            # shared by /facts and HTMX partials
  kpi.html                    # single KPI card
dashboard.html
ontologies.html
ontology_versions.html
ontology_version.html
extraction_profile.html
facts.html
quarantine.html
projection.html
audit.html
```

## Static directory

`services/ontology_ui/static/`

- `style.css` — design system tokens + component styles. No build step.
- `htmx.min.js` — pin to HTMX 1.9.10. Save verbatim; do not edit.
  (The fixer lane fetches the official build and saves it as part of
  the setup script. The designer lane references `/static/htmx.min.js`
  in `base.html`.)

## API client contract

`services/ontology_ui/api_client.py` exposes an `OntologyClient` class
with a single async `httpx.AsyncClient`. Methods:

- `health() -> dict`
- `list_ontologies() -> list[dict]`
- `list_versions(ontology_id) -> list[dict]`
- `get_version(ontology_id, version) -> dict | None`
- `extraction_profile(ontology_id, version) -> dict`
- `list_facts(ontology_id: str | None = None) -> list[dict]`
- `list_quarantine() -> list[dict]`
- `list_sync() -> list[dict]`
- `audit() -> list[dict]`

Construction takes the API base URL. The FastAPI dependency
Each request resolves the opaque cookie against the in-memory session store
and returns a per-request `OntologyClient` bound to its token + workspace.
The ontology API remains authoritative: configured `ONTOLOGY_WORKSPACE` is
the default/single served workspace, and a bearer principal must also list
that workspace in `ONTOLOGY_TOKENS`. A caller-supplied `X-Workspace` cannot
select a different workspace; mismatch is rejected with 403. The API/UI
deployment must use the same workspace configuration.

A `UpstreamError` exception class with `.status_code` and `.detail` is
raised for any non-2xx; the route layer maps it to the appropriate
response.

## Tests

`services/ontology_ui/tests/test_app.py` uses `unittest` and
`fastapi.testclient.TestClient`. Mocks the upstream via httpx
`MockTransport` (or a fixture `monkeypatch`-installed in the
dependency). Cover at least:

- `/health` returns 200 when upstream healthy.
- `/` redirects to `/login` without a session.
- `/login` POST with valid form sets the session and redirects to `/`.
- `/login` POST with too-short token returns 422.
- `/facts` carries `Authorization` and `X-Workspace` from session.
- Upstream 401 clears the session and redirects to `/login?next=/facts`.
- Upstream 502 renders the "upstream unavailable" panel.

## Design handoff discipline (for the designer lane)

- Pick the **visual** and **layout** system. The fixer lane must
  preserve the markup, classes, ids, and partial structure verbatim.
- The designer does not need to author or polish user-facing copy;
  the orchestrator will review copy after integration. Use grounded,
  normal wording in the templates (e.g. "No ontologies yet", "Last
  projected", etc.). No marketing flourishes.
- Use system font stack and CSS variables. No external fonts.
- Dark mode is welcome but optional. If implemented, ship both
  themes; do not require JS to enable dark.
- Tables must be responsive on small screens (horizontal scroll
  inside a wrapper, not on the page).

## Compose integration (fixer lane only)

Add to `compose.ontology.yaml`:

```yaml
  ontology-ui:
    build:
      context: .
      dockerfile: services/ontology_ui/Dockerfile
    restart: unless-stopped
    ports:
      - "127.0.0.1:${ONTOLOGY_UI_PORT:-8020}:8010"
    environment:
      ONTOLOGY_API_URL: http://ontology:8010
      ONTOLOGY_UI_HTTPS_ONLY: ${ONTOLOGY_UI_HTTPS_ONLY:-}
    networks: [knowledge_db]      # only needs API access; no DB / no ontology_backend
    depends_on:
      ontology:
        condition: service_healthy
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health', timeout=5)"]
      interval: 15s
      timeout: 5s
      retries: 10
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

The new service should NOT join `ontology_backend`; it only calls
the API, so DB and LightRAG stay unreachable from the UI container.

## File ownership

| File / path                                           | Lane       |
|-------------------------------------------------------|------------|
| `services/ontology_ui/static/style.css`               | designer   |
| `services/ontology_ui/static/htmx.min.js`             | designer (download + commit verbatim) |
| `services/ontology_ui/templates/**`                   | designer   |
| `services/ontology_ui/app.py`                         | fixer      |
| `services/ontology_ui/api_client.py`                  | fixer      |
| `services/ontology_ui/auth.py`                        | fixer      |
| `services/ontology_ui/filters.py`                     | fixer      |
| `services/ontology_ui/__init__.py`                    | fixer      |
| `services/ontology_ui/requirements.lock`              | fixer      |
| `services/ontology_ui/Dockerfile`                     | fixer      |
| `services/ontology_ui/tests/**`                       | fixer      |
| `compose.ontology.yaml` (edit)                        | fixer      |
| `.env.example` (edit)                                 | fixer      |
| `scripts/init_env.py` (edit)                          | fixer      |
| `README.md` (edit)                                    | fixer      |
| `docs/ontology/runbook.md` (edit)                     | fixer      |

The contract file (`CONTRACT.md`) is the source of truth; both lanes
must follow it. The two lanes do not touch the other's files. The
orchestrator reconciles the result and runs the tests.
