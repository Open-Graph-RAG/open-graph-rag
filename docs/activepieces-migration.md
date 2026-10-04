# Activepieces migration and recovery

Status: replacement acceptance is pending. Keep n8n running and its callers
unchanged until all gates below pass. Activepieces 0.92.1 is the target; image
publication is not proof that the required Community Edition runtime works.

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
