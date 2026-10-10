# Ontology operations runbook

## Enable governed mode

For a new checkout, run `python3 scripts/init_env.py`; it creates `.env` once and generates admin/reader tokens with workspace grants. If `.env` already exists, keep its database/provider secrets and add or update these settings manually:

```dotenv
ONTOLOGY_WORKSPACE=company_governed
ONTOLOGY_TOKENS='{"<admin-token>":{"role":"ontology_admin","actor":"local-admin","workspaces":["company_governed"]},"<reader-token>":{"role":"ontology_reader","actor":"local-reader","workspaces":["company_governed"]}}'
```

Tokens must be at least 32 characters. Generate fresh values on the deployment host with `python3 -c 'import secrets; print(secrets.token_hex(32))'`; store them in the mode-0600 `.env` and pass them to clients through a secret manager. Do not reuse the sample placeholders. The `ONTOLOGY_WORKSPACE` value must match each token's `workspaces` grant. The overlay gives LightRAG a dedicated workspace so existing ungoverned graph content in the legacy workspace does not appear in governed retrieval.

Start the governed services with `docker compose -f compose.yaml -f compose.ontology.yaml up -d --build`. The ontology API is at `http://127.0.0.1:${ONTOLOGY_PORT:-8010}` and retrieval gateway at `http://127.0.0.1:${LIGHTRAG_PORT:-9621}`. Check `docker compose -f compose.yaml -f compose.ontology.yaml ps` before using the API.

Ontology API requests use both a role token and workspace header:

```sh
export ONTOLOGY_ADMIN_TOKEN='<admin-token>'
export ONTOLOGY_WORKSPACE='company_governed'
curl -H "Authorization: Bearer $ONTOLOGY_ADMIN_TOKEN" \
  -H "X-Workspace: $ONTOLOGY_WORKSPACE" \
  http://127.0.0.1:8010/v1/ontologies
```

Definitions may be sent as JSON objects or as YAML text inside a JSON `definition` string. For example, load the bundled YAML without installing a YAML parser on the client, then create and validate it:

```sh
python3 -c 'import json; print(json.dumps({"definition":open("ontology/definitions/ogr-core/1.0.0.yaml").read()}))' > /tmp/ogr-core.json
curl -H "Authorization: Bearer $ONTOLOGY_ADMIN_TOKEN" \
  -H "X-Workspace: $ONTOLOGY_WORKSPACE" -H 'Content-Type: application/json' \
  --data-binary @/tmp/ogr-core.json http://127.0.0.1:8010/v1/ontologies
curl -H "Authorization: Bearer $ONTOLOGY_ADMIN_TOKEN" \
  -H "X-Workspace: $ONTOLOGY_WORKSPACE" -H 'Content-Type: application/json' \
  --data-binary @/tmp/ogr-core.json http://127.0.0.1:8010/v1/ontologies/ogr-core/validate
curl -H "Authorization: Bearer $ONTOLOGY_ADMIN_TOKEN" \
  -H "X-Workspace: $ONTOLOGY_WORKSPACE" -H 'Content-Type: application/json' \
  -d '{"version":"1.0.0"}' http://127.0.0.1:8010/v1/ontologies/ogr-core/publish
```

Publishing the first version makes it active. Publishing a later version records it as published but leaves the current version active; use the migration plan/apply workflow below to move existing facts and activate the target version. Only admins can create, publish, and migrate. Readers can validate definitions and read ontology data.

Submit an entity fact before relations that reference it. `POST /v1/facts/validate` checks a fact without storing it; `POST /v1/facts` with body `{"fact":{...},"mode":"enforce"}` validates and stores accepted facts. Relation facts include `subject_id`, `predicate`, `object_id`, properties and non-empty provenance entries with `document_id` and `source_id`. `GET /v1/facts`, `GET /v1/quarantine`, `GET /v1/projection-status`, and `GET /v1/ontology-audit` show canonical data, quarantined candidates, projection status and audit history. Extraction profiles are available at `GET /v1/ontologies/{id}/versions/{version}/extraction-profile`.

Enforcement mode is explicit per fact write: `enforce` rejects invalid facts, `observe` records violations without accepting them into the governed fact store, and `quarantine` keeps reviewable invalid candidates out of the governed graph. Under quarantine mode, rules configured as `reject` still reject; other violations are quarantined. Schema-level policy labels do not silently override the selected request mode.

## Health and projection state

Check `/health` on the ontology API and retrieval gateway. A successful gateway health response means the gateway can reach LightRAG; it does not prove that every canonical fact is projected. Inspect `GET /v1/projection-status` for outbox attempts, `delivered_at`, and `last_error`. Retryable LightRAG failures return to the store's outbox retry schedule; do not mark them complete manually.

For a one-shot batch, the projector leases only rows for the configured `LIGHTRAG_WORKSPACE`, projects each canonical fact under a migration guard, then calls `complete_outbox(id, lease_token)` or `fail_outbox(id, error, lease_token)`. Reprocessing is safe because labels are deterministic and the adapter uses LightRAG's graph entity/relation edit routes to recover from an already-created item. Inspect the canonical record and `canonical_fact_id` in the fact node description before manually repairing a persistent error.

## Adding or changing a vocabulary

Validate the draft, publish a new version, and check `GET /v1/ontology-audit`. For a breaking change, produce and review a migration dry run, take a PostgreSQL backup, apply the mapping, then monitor outbox reconciliation. `POST /v1/ontologies/{id}/migrations/rollback` restores canonical state and queues projection reconciliation; LightRAG itself is not an authoritative rollback source.

## Security boundary

In governed mode, retrieval clients use the gateway with `X-API-Key: $LIGHTRAG_API_KEY`. Ontology clients use `Authorization: Bearer $ONTOLOGY_ADMIN_TOKEN` (or a reader token) and `X-Workspace: $ONTOLOGY_WORKSPACE`; generated tokens grant access only to that configured workspace. Keep the LightRAG API key only in service configuration; the gateway checks the caller key and sends the configured upstream secret. The projector needs access to LightRAG's private backend network. The compose override assigns LightRAG a dedicated `ONTOLOGY_WORKSPACE`, so legacy workspace content is excluded from governed retrieval. Do not publish LightRAG's port or connect raw-ingestion clients to its backend network when governance is required.

## Accessing the console

The `ontology-ui` service is a read-only browser console for the operations covered above. It is reachable at `http://127.0.0.1:${ONTOLOGY_UI_PORT:-8020}` and is part of the governed compose overlay. The host port (default `8020`) is intentionally distinct from the ontology API host port (default `8010`) so the API and console can run side-by-side. Sign in with the same bearer token (`ONTOLOGY_ADMIN_TOKEN` or a reader token) and the workspace name. The token is kept in a signed session cookie scoped to the console and is never persisted beyond it. The console has no write endpoints; all mutations happen through the API or `docker compose exec` so the same audit trail applies. Set `ONTOLOGY_UI_HTTPS_ONLY=1` and put the service behind TLS before exposing it outside the local host.

If legacy content needs to remain searchable, move it through the accepted canonical fact pipeline into the governed workspace. Existing LightRAG-only graph content has no guaranteed ontology classification and is not automatically governed. `GET /v1/quarantine` lists quarantined or observed candidates. `GET /v1/ontologies/{id}/versions/{version}/extraction-profile` returns a prompt aid derived from ontology descriptions; it does not enforce extraction.

This boundary governs facts accepted through the ontology service and blocks client bypasses in the governed compose mode. It cannot claim that LightRAG's own probabilistic extraction obeys the ontology. Raw documents handled outside the canonical fact pipeline remain uncontrolled and should not be presented as ontology-validated facts.
