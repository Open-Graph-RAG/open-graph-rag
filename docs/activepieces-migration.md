# Activepieces migration and recovery

Status: isolated mocked-engine and local acceptance passed on Activepieces
0.92.1. Production cutover remains deferred: keep n8n and callers unchanged
until credentials, caller migration and isolated restoration gates pass.

## Baseline

A read-only comparison on 2026-10-04 found two installed n8n workflows:

| Workflow | Installed activation | Replacement activation after acceptance |
|---|---|---|
| Internet prompt to LightRAG | Active | Active |
| NotebookLM exported Google Docs to LightRAG | Disabled | Disabled |
| Product knowledge demo (11 documents) | Repository only | Disabled/manual |

Installed node parameters match the repository exports. Credential references
are separate and must be reconnected privately. Execution history and the
n8n owner account stay in the recovery archive; they are not translated.
Both live workflows use execution order `v1`, retain manual executions, use
fixed time-saving mode, restrict workflow callers to the same owner, and are
unavailable through n8n MCP. Map those controls privately before cutover;
do not copy platform-specific settings blindly.

## Acceptance gates

The default `compose.yaml` continues to run n8n. The optional
`compose.activepieces.yaml` adds dedicated Activepieces PostgreSQL/pgvector,
Redis, app/worker and local gateway services. The gateway makes
`activepieces.localhost` usable from both the host browser and Docker workers,
so the UI and generated webhook URLs work in the local stack. Prepare missing
settings explicitly:

```bash
python3 scripts/add_activepieces_env.py
docker compose -f compose.yaml -f compose.activepieces.yaml config --quiet
```

The migration command preserves existing `.env` bytes, values and file mode,
prints no secret values, and does nothing on repeat execution. Initial setup
with `scripts/init_env.py` also generates these settings. If you change
`ACTIVEPIECES_PORT`, update `AP_FRONTEND_URL` to the same `activepieces.localhost`
port. The gateway listens only on host loopback. Protect `AP_ENCRYPTION_KEY`
and `AP_JWT_SECRET`.

After feasibility checks pass, the additive service commands are:

```bash
docker compose -f compose.yaml -f compose.activepieces.yaml up -d activepieces-gateway activepieces-app activepieces-worker
docker compose -f compose.yaml -f compose.activepieces.yaml ps
docker compose -f compose.yaml -f compose.activepieces.yaml stop activepieces-gateway activepieces-app activepieces-worker
```

Starting the optional services is not a cutover. No caller should use their
webhooks before acceptance. Review/redact logs locally before sharing them.

Before porting flows, import and run a minimal native flow on the pinned
Community Edition image. Prove header authentication rejects unauthorized
requests before downstream calls, `/sync` supports custom JSON responses,
`SANDBOX_CODE_ONLY` runs generated self-contained JavaScript, a worker can
reach LightRAG, and self-hosted Google OAuth can export a document. Record
tested piece versions. Test malformed requests, wrong methods, authentication
failures and engine timeouts separately: native responses may differ from
explicit workflow errors.

Successful ingestion preserves response fields and returns `202` when any
document is queued, or `200` for duplicate-only/skipped outcomes. Explicit
workflow errors use JSON with `400` for invalid input, `405` for wrong methods,
and `502` for upstream failures. Do not equate every LightRAG `409` with a
duplicate. Report partial acceptance and every accepted tracking ID. Disable
automatic insertion retries; retries cannot promise exactly-once ingestion.

The explicit error contract to test is:

```json
{
  "status": "error",
  "error": {"code": "invalid_input", "message": "Invalid request input."},
  "queued": 0,
  "alreadyPresent": 0,
  "trackIds": []
}
```

Codes are `invalid_input`, `method_not_allowed`, and `upstream_failure`.
Messages are stable redacted text; upstream error bodies and credentials must
not appear. On partial acceptance, counts and tracking IDs reflect successful
inserts before the failure. Native trigger/engine errors require separate
observed contracts and may bypass this workflow response.

Run three separate verification layers:

1. Behavioral unit tests and generated-export drift checks.
2. Imported flows in the actual engine with mocked Tavily, Google and LightRAG,
   asserting both HTTP responses and downstream requests.
3. An isolated local stack with fictional data, separate volumes/credentials/
   ports/networks/workspace, isolated Ollama `bge-m3` and `qwen2.5:3b`, bounded
   tracking-ID completion, source-bearing LightRAG/MCP retrieval, and a real
   LibreChat UI request that calls the knowledge tool.

Local tests must reject production mounts/endpoints and paid-provider fallback.
Verify model tool calling before the full test. Preserve redacted evidence;
do not assert exact model wording. Credential-dependent production checks
remain distinct from mocked acceptance and local E2E.

## Credentials and callers

Store outbound Tavily, Google OAuth and LightRAG credentials in encrypted
Activepieces connections. Incoming header-authentication tokens belong to the
trigger configuration, so flow exports can contain secrets even when outbound
connections are encrypted. Sanitize exports, execution logs, screenshots and
backups. Back up encryption keys with their databases in protected storage.

Inventory each webhook caller privately: owner, current route, generated new
Activepieces URL ending in `/sync`, request method, token source, client/proxy
timeout, retry policy and a verified test result. Never construct a webhook URL
from a workflow name. Bound execution below the engine's synchronous deadline
and configure caller timeouts consistently. If any caller or credential is
unresolved, defer cutover.

## Cutover

Only proceed after replacement acceptance and isolated restoration pass:

1. Pause incoming requests at the callers/routing layer and drain n8n executions.
2. Stop n8n without removing its volume. Capture its complete volume, SQLite
   database, binary files, credential-encryption configuration, Compose files,
   protected environment and image identity in a secure recovery archive.
3. Restore that archive into an isolated project with separate ports, networks
   and volumes. Block outbound ingestion and verify workflows, credentials,
   owner access and history without replaying executions.
4. Enable Activepieces web ingestion. Keep Google Docs and demo disabled.
5. Switch only verified callers to their generated URLs, then resume traffic.

Do not run `docker compose down -v`. Archive validation is mandatory; a backup
command succeeding is insufficient evidence of recoverability.

## Rollback

Pause traffic, drain Activepieces work, and disable its ingestion flow. Restore
n8n routing and its prior activation state, then resume traffic after checking
the route. Preserve both platforms' databases and volumes. Reconcile tracking
IDs and partial acceptance privately; do not automatically resubmit requests.

## Upgrades and troubleshooting

Upgrade app and worker together using a newly verified matching image digest.
Retest piece versions, sandbox behavior, imports, OAuth, native error responses
and synchronous deadlines before using an upgrade. Back up and test restoration
first. An unavailable connection, timeout, sandbox failure or unverified model
tool call blocks acceptance; it does not justify bypassing authentication or
switching the local E2E stack to an external provider.

## Observed feasibility and acceptance limits

The isolated 0.92.1 engine accepted native imports and publishing, executed
`SANDBOX_CODE_ONLY` code with neither `require` nor `fetch`, and reached mocked
providers and LightRAG through the native HTTP piece. Tested pieces are
`@activepieces/piece-http` 0.12.1, `@activepieces/piece-webhook` 0.1.42,
and `@activepieces/piece-http-oauth2` 0.3.0.
Custom synchronous responses returned the requested 202 status. A disposable
OAuth connection was persisted as encrypted `{data,iv}` ciphertext; connection
APIs and the flow template export omitted its tokens and client secret. The Google export request uses the native HTTP OAuth2 piece. Native mocked token exchange and refresh passed: a one-second authorization
code token expired, the engine refreshed it, and the authenticated export
queued one document with a 202 response. The mock verified the client, grant
and configured `/redirect` URI. Real Google consent and credential-dependent
verification still require production credentials.

Observed native responses must not be confused with the workflow's explicit
400/405/502 contract:

| Native condition | Observed response | Downstream behavior |
| --- | --- | --- |
| Missing or wrong header token | 500, generic JSON | Trigger failed; zero downstream steps |
| Malformed JSON | 400, JSON parser error | Request rejected before execution |
| HTTP action timeout | 500, no flow response returned | HTTP action failed |
| 30-second `/sync` deadline | 408, `{}` | The flow continued and later succeeded |

The gateway overwrites `X-Activepieces-Request-Start` with its receive timestamp.
Synchronous flow guards must include queueing time, stop starting five-second
insertions after twenty seconds, and return partial accepted counts and IDs.
A native 408 is an indeterminate outcome: preserve tracking evidence and do
not automatically resubmit. These guards do not promise exactly-once ingestion.

The local profile uses its own volumes, ports, credentials and workspace. Both
LightRAG and LibreChat use isolated Ollama with `bge-m3` and `qwen2.5:3b`.
The exact Qwen weights are retained; the local initializer adds `num_thread 4`
to avoid observed CPU thread contention. Extraction uses a 768-token output
cap, one concurrent LLM call, and unchanged provider/worker deadlines. Earlier
failed workspaces remain available for comparison. The first eleven-document
run failed a 480-second extraction timeout; a later single CP-263 probe
processed in 181 seconds and passed independent source-bearing MCP retrieval
in 24.3 seconds. This direct probe proves backend capability, not Activepieces
ingestion or the full eleven-document acceptance gate.

`tests/e2e/preflight.py` checks resolved configuration and runtime isolation.
Use `--project activepieces-local-e2e --env-file /tmp/<private-file>` with
`--local-overlay tests/e2e/compose.local.yaml` and the explicit isolated port
allowlist, then repeat with `--runtime`. It rejects production mounts/endpoints,
foreign volumes/networks, writable binds, and missing shared networks needed
by model/tool callers. LibreChat's custom-only provider configuration supplies
the explicit local base URL; environment reverse-proxy settings alone are not
a substitute for testing the actual UI/tool route.

`tests/e2e/local_tool_smoke.py --tracking-file /tmp/<accepted-ids.json>` polls
each accepted ID to processed state under a bounded deadline and checks source
evidence through MCP. `--ingest-fixture` is a separate direct backend probe;
it checkpoints each accepted ID privately before the next insertion.
Resume with `--tracking-checkpoint` without resubmitting documents. Neither
this smoke nor a model-only tool-call test replaces imported-flow acceptance
or a real LibreChat UI request. Cutover remains deferred until every gate and
caller/credential check passes.


## Isolated engine run and resource recovery

Run the model capability check after the isolated services needed for ingestion
are running, so loading both models tests the actual memory budget. Stop the
mock project after its acceptance run before starting the real local run. The
model-only check performed with fewer services does not prove sufficient memory
for both projects at once. Leave production containers untouched.

The eleven-document Activepieces demo returned 202 with eleven distinct tracking
IDs in the fresh `ap_e2e_engine_v4` workspace. Initial indexing failed for two
records when the Ollama child process was killed under global memory pressure.
An embedding worker timeout then failed another record and cancelled the rest;
all eleven records ultimately failed that indexing attempt.
Stopping only the disposable local Activepieces app, worker and gateway after
its completed response freed memory; their databases and volumes were retained.
All eleven original tracking IDs reached processed state after the serial
recovery. Source-bearing LightRAG and MCP retrieval passed for both CP-263/UNASSIGNED
and CP-248/proposal approval, and the local model emitted and executed the
knowledge tool. A fresh real LibreChat UI request called `knowledge_search` against this
workspace and returned successful evidence with `UNASSIGNED` and the actual
CP-263 source filename; the tool call took 41 seconds. Redacted fictional
UI evidence was captured. Generated wording was not used as an assertion.

The pinned LightRAG `/documents/reprocess_failed` endpoint accepts an explicit
recovery request for existing records and preserves their original tracking IDs.
An initial controlled recovery did not complete. After setting embedding
batch size and concurrency to one, configuring both models with four threads,
and passing the model capability check within the unchanged embedding deadline,
a second explicit recovery was requested. Its bounded tracking poll completed all eleven records. No documents were resubmitted and no ingestion retry was enabled. Preserve the initial failure alongside any recovery result;
never treat a recovery as evidence that the first run succeeded.

## Native trigger history and incoming tokens

Activepieces 0.92.1 native header authentication rejects unauthorized requests
before downstream actions, but the trigger payload retained in execution
history includes request headers and the incoming token. Restrict run-history
and API access and protect raw database backups accordingly. Sanitize tokens
from shared exports, logs, screenshots and evidence. Deadline code receives
only the gateway timestamp, so it does not copy authentication headers into
additional step inputs. Outbound credentials remain encrypted connections;
this does not encrypt the incoming token in native trigger configuration or
execution history.

## Pinned runtime source references

- [Activepieces 0.92.1 step error views](https://github.com/activepieces/activepieces/blob/0.92.1/packages/server/engine/src/lib/handler/context/flow-execution-context.ts): generated failure branches read `step.error.message`.
- [Native webhook authentication](https://github.com/activepieces/activepieces/blob/0.92.1/packages/pieces/core/webhook/src/lib/triggers/catch-hook.ts): authenticates before returning the retained trigger payload.
- [Connection expressions](https://github.com/activepieces/activepieces/blob/0.92.1/packages/server/engine/src/lib/variables/connection-token.ts): project connections use the supported dot expression form.

## Final isolated acceptance result

Independent full acceptance passed through freshly imported and published
flows on the pinned engine after correcting failure expressions to the native
`error.message` step view. It asserted exact successful responses, invalid
input, native authentication/malformed responses, wrong methods, filtering,
provenance, deduplication, insertion order, duplicate and unrelated conflicts,
provider failures, partial acceptance, tracking, Google export and all eleven
demo documents. Config and runtime isolation preflights passed with strict
mock worker network mode and the exact fixture IP allowlist.

Local acceptance passed after the documented explicit recovery: all eleven
original IDs processed, both expected source-bearing retrieval cases passed
through LightRAG and MCP, and a real LibreChat UI request called the knowledge
tool against the completed fixture. This recovered run is not a clean first
pass. Production credential, caller and isolated restoration checks remain
unverified, so neither n8n activation nor live routing was changed.
