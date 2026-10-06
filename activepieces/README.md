# Activepieces ingestion migration

The replacement is gated. Keep n8n and its callers running until the
[migration acceptance and recovery gates](../docs/activepieces-migration.md)
pass. Starting the optional Activepieces services does not switch traffic.

## Setup and imports

Run `python3 scripts/add_activepieces_env.py` to add missing settings to the
existing `.env` without rewriting existing values or displaying secrets. Then
use `docker compose -f compose.yaml -f compose.activepieces.yaml up -d
activepieces-postgres activepieces-redis activepieces-app activepieces-worker
activepieces-gateway`. Open the configured localhost UI address.

Import the sanitized native template JSON files from `workflows/`. Configure
the incoming header secret separately in each webhook trigger and reconnect
outbound credentials using encrypted Activepieces connections. Exported
templates are not credential backups. The generic HTTP piece has no connection
picker: its expressions resolve project connections by these external IDs:
`tavily_bearer_api_key`, `lightrag_api_key`, and `google_drive_oauth2`.
The first two use encrypted `SECRET_TEXT` connections. Google requires an
OAuth connection and verified refresh support before it can be enabled.
Create or reconnect these privately through the supported connection API;
changing a display name does not change the external ID. The E2E importer is
restricted to isolated test gateways and must not be used for production. Native trigger configuration contains
incoming secrets after setup: sanitize exports and secure database backups.
Copy the generated webhook URL and append `/sync`; verify the caller's method,
timeout and retry policy before any routing change. Keep Google Docs and the
manual demo disabled at cutover.

The manual demo embeds the fictional eleven-document dataset. Run it once,
save every accepted tracking ID, and poll each to completion before querying.
A queued response confirms acceptance, not completed indexing. Duplicate-only
outcomes confirm stored source identity; use saved tracking IDs to check its
indexing state. Review partial
acceptance before any deliberate retry; never resubmit automatically.

## Validation and maintenance

Pure helpers are the source for generated code steps. Run:

```sh
node --test activepieces/helpers.test.cjs activepieces/workflows.test.cjs n8n/tests/*.test.cjs
python3 activepieces/generate_workflows.py --check
python3 -m unittest discover -s scripts/tests -v
```

The `tests/e2e/` harness separates mocked engine acceptance from real isolated
Ollama/LightRAG/LibreChat checks. Credential-dependent Google consent and live
caller verification are additional gates. See the migration document for
observed native errors, timeout behavior, isolated test commands, secure
backup/restoration, upgrade checks and rollback. A synchronous engine timeout
can leave work running; inspect tracking and executions before retrying.

## Isolated mock acceptance

Use a disposable Activepieces project and a mode-`0600` Compose env file under
`/tmp` with fresh database, encryption and JWT values. Keep the real `.env` and
production credentials out of this project. Set `AP_ACCESS_TOKEN` from the
disposable local session, `AP_PROJECT_ID` to that session's project, and these
test-only fixture variables in a private shell: `AP_E2E_TAVILY_TOKEN`,
`AP_E2E_LIGHTRAG_TOKEN`, `E2E_LIGHTRAG_API_KEY`, `E2E_WEBHOOK_TOKEN`, and
`E2E_NOTEBOOK_TOKEN`. The first three must match the mock fixture values; the
two webhook values can be any test-only strings. The importer needs
`AP_ACCESS_TOKEN`, `AP_PROJECT_ID`, both `AP_E2E_*` connection values and both
webhook values. The acceptance runner needs the two webhook values and
`E2E_LIGHTRAG_API_KEY`.

Use `ACTIVEPIECES_PORT=18080` and
`AP_FRONTEND_URL=http://activepieces.localhost:18080`; set base service ports
to the isolated allowlist (for example `LIBRECHAT_PORT=13080`,
`LIGHTRAG_PORT=19622`, `N8N_PORT=15678`). Set `AP_TEST_COMPOSE_ENV`
to the private Compose env-file path, then validate
and start only the isolated mock project:

```sh
python3 tests/e2e/preflight.py --project ap-migration-probe --env-file "$AP_TEST_COMPOSE_ENV"
docker compose -p ap-migration-probe --env-file "$AP_TEST_COMPOSE_ENV" \
  -f compose.yaml -f compose.activepieces.yaml -f tests/e2e/compose.mock.yaml \
  up -d activepieces-postgres activepieces-redis activepieces-app \
  activepieces-worker activepieces-gateway mock-integrations
python3 tests/e2e/preflight.py --project ap-migration-probe \
  --env-file "$AP_TEST_COMPOSE_ENV" --runtime
```

Import and enable the internet, Google and temporary demo-webhook flows. The
Google connection performs a mock authorization-code exchange; the mock expires
that access token quickly so the export action exercises refresh. Copy the
three `flowId` values from the importer's output into the variables below:

```sh
python3 tests/e2e/import_activepieces.py \
  --base-url http://127.0.0.1:18080 \
  --mock-url http://mock-integrations:9621 \
  --project-id "$AP_PROJECT_ID" --enable --acceptance-demo-webhook

export INTERNET_FLOW_ID='<internet flowId>'
export GOOGLE_FLOW_ID='<Google flowId>'
export DEMO_FLOW_ID='<demo flowId>'
python3 tests/e2e/acceptance.py \
  --webhook-url "http://127.0.0.1:18080/api/v1/webhooks/$INTERNET_FLOW_ID/sync" \
  --google-url "http://127.0.0.1:18080/api/v1/webhooks/$GOOGLE_FLOW_ID/sync" \
  --demo-url "http://127.0.0.1:18080/api/v1/webhooks/$DEMO_FLOW_ID/sync" \
  --demo-webhook --mock-url http://127.0.0.1:19621
```

The runner resets only the disposable mock fixture. Do not run it concurrently
with another mock acceptance. Shut down the probe without removing its volumes:

```sh
docker compose -p ap-migration-probe --env-file "$AP_TEST_COMPOSE_ENV" \
  -f compose.yaml -f compose.activepieces.yaml -f tests/e2e/compose.mock.yaml down
```

## Isolated local demo

For the real local LightRAG path, use the separate `activepieces-local-e2e`
project and `tests/e2e/compose.local.yaml`. Its private Compose env file must
contain local-only service URLs and fresh credentials, with
`ACTIVEPIECES_PORT=8081`, `AP_FRONTEND_URL=http://activepieces.localhost:8081`,
`LIBRECHAT_PORT=13080`, `LIGHTRAG_PORT=19622`, and `N8N_PORT=15678`
(the legacy service stays outside the local profile). Set `AP_LOCAL_COMPOSE_ENV`
to that file, set `AP_ACCESS_TOKEN` and `AP_PROJECT_ID` from the local
Activepieces session, and set `AP_E2E_LIGHTRAG_TOKEN` to the private local
LightRAG API key. Preflight and start the local fixture:

```sh
python3 tests/e2e/preflight.py --project activepieces-local-e2e \
  --env-file "$AP_LOCAL_COMPOSE_ENV" --local-overlay tests/e2e/compose.local.yaml
docker compose -p activepieces-local-e2e --env-file "$AP_LOCAL_COMPOSE_ENV" \
  -f compose.yaml -f compose.activepieces.yaml -f tests/e2e/compose.local.yaml up -d
python3 tests/e2e/preflight.py --project activepieces-local-e2e \
  --env-file "$AP_LOCAL_COMPOSE_ENV" --local-overlay tests/e2e/compose.local.yaml --runtime
```

Before importing, run `tests/e2e/model_smoke.py` using a Python environment
with the pinned MCP dependencies. It verifies both isolated models, a real
knowledge tool call, and the generation-to-embedding switch within the worker
deadline. A failure blocks the test; use no external provider fallback.

Import the demo without adding Tavily or Google connections. This keeps its
LightRAG URL on the local service; open the imported disabled flow in
Activepieces and run it manually after confirming the local fixture is ready:

```sh
python3 tests/e2e/import_activepieces.py \
  --base-url http://127.0.0.1:8081 \
  --mock-url http://mock-integrations:9621 \
  --project-id "$AP_PROJECT_ID" --flow product-knowledge-demo --local-demo
```

Save every accepted tracking ID privately in a JSON object with `trackIds`.
Set `LIGHTRAG_API_KEY` and `MCP_TOKEN` to the isolated values and run:

```sh
python tests/e2e/local_tool_smoke.py --tracking-file /tmp/<private-tracking-file>.json \
  --full-fixture --track-deadline 3600
```

Then send a real LibreChat UI request through the local Ollama endpoint that
calls `knowledge_search`, and capture redacted source-bearing tool evidence.
Do not resubmit the demo or retry failed insertions automatically.
