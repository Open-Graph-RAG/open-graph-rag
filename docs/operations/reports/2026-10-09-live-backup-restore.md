# Live backup and sandbox restore verification — 9 October 2026

**Result:** the live local deployment was backed up, both encrypted repositories were verified,
and the snapshot was restored successfully into an isolated sandbox. All recorded database
fingerprints and file-volume fingerprints matched. This is a live-data drill, not the synthetic CI fixture.
The drill required a manual pause of one orphaned writer and found an MCP configuration drift;
those findings prevent treating this run as proof of completely unattended, exact application recovery.

## Execution and recovery points

- Stack revision: `ad7cdadb5aaf2d85b7ec98a0ca5afdbb6cf8ca05`.
- Configuration: `compose.yaml` plus `compose.activepieces.yaml`.
- Backup ID: `1289dd66-c429-43f2-b255-1e8d499527a9`.
- Primary snapshot: `e6568d7f98aabc8ae4fe2a07b83dd793345c936108d44fdfa50d38311ad2cb9f`.
- Secondary snapshot: `5bfba5bb8a2c037beaca7f27d38f0e36d3422f509d6a155aa922c879326a03b7`.
- Backup completed: 2026-10-09T11:57:29+02:00.
- Backup orchestration, including service restart: **166.312 seconds**.
- Isolated data restore and fingerprint comparison: **149.007 seconds**.
- Snapshot payload: **268,787,623 bytes** in **65 files**.
- Sandbox project: `ogr-restore-cd6e6ece9b`.

These timings measure this local dataset and the implemented data validation; they do not measure
full user-facing recovery with all applications and external providers. Exact per-service interruption
was not recovered from retained Docker events. All 13 original services were running at the final check;
services with health checks reported healthy.

## Backup verification evidence

Both repositories passed `restic check --read-data`. The backup engine also restored the new snapshot
from each repository and checked its manifest, artifact checksums and archive safety before declaring
it complete. A separate verify-only invocation succeeded in 1.355 seconds.
The secondary repository is a separate **local mirror on the same host**, not an offsite recovery copy.
Remote storage, S3 credentials and object locking were not tested.

The [machine-readable evidence](2026-10-09-live-backup-restore.evidence.json) contains snapshot IDs,
dump sizes/checksums, every table/collection count and SHA-256 fingerprint, and every volume fingerprint.
No credentials, decrypted values, documents or conversation text are included in that evidence.

## Restored data evidence

The restore compared every captured public PostgreSQL table and application MongoDB collection with
its backup-time count and deterministic row fingerprint. Empty tables/collections were included.

| Store | Tables / collections | Records |
| --- | ---: | ---: |
| `activepieces-postgres` | 81 | 13,929 |
| MongoDB `LibreChat` | 54 | 377 |
| `postgres` | 16 | 47,360 |

Selected recovered records:

- LightRAG: **96 full documents**, **96 document-status records**,
  **5,372 chunks**, **5,538 graph nodes** and **7,694 graph edges**.
- Both vector generations were preserved: **1** BGE-M3 chunk embedding and **473** OpenAI chunk embeddings,
  plus the corresponding entity/relation vector tables.
- LibreChat: **1 user**, **11 conversations** and **73 messages**.
- Activepieces: **2 flows**, **4 flow versions**, **4 flow runs** and **1 user**.
- n8n: SQLite `PRAGMA integrity_check` returned **`ok`**; **2 workflows**,
  **4 credentials**, **14 executions** and **1 user** were recovered.
  An isolated n8n CLI export decrypted **all 4 credentials** successfully.
  Plaintext was confined to the helper container's temporary filesystem and was not exported into the report.
- Activepieces had **0 stored app connections**; there were no live connection credentials to decrypt.

All seven backed-up file volumes matched their content, modes, numeric ownership and link fingerprints
before the extra application checks. The original uploads/images volumes contain no uploaded files,
so this run verifies their empty state rather than recovery of populated media.

| File volume | Entries matched | SHA-256 fingerprint |
| --- | ---: | --- |
| `activepieces_redis_data` | 6 | `0160a055326119deb66647de4915e645e02729443e3794f7b96a56d5023f355c` |
| `librechat_data` | 2 | `22682839190134889a3441f2570b5fec490c06847dd653eb0ad3ad177ed30c6b` |
| `librechat_images` | 1 | `06fc33591079c5bb98a5ad9120f15d631e15b784f60f2aa7053f444d2a9cfb59` |
| `librechat_uploads` | 1 | `06fc33591079c5bb98a5ad9120f15d631e15b784f60f2aa7053f444d2a9cfb59` |
| `lightrag_inputs` | 15 | `ba9f9a81ffdd4fb7b9f8d8a3da61d3efd13f424612c1c77b8c3cbc05276b2fd1` |
| `lightrag_storage` | 5 | `fa2fa710a82d1bbdf53802832c68bda23550aa1fe3fc8276792ddc0fdf13a59b` |
| `n8n_data` | 12 | `7e9071b1acdfeb477495d2e33482d3bc0210e20cab4575b9c0bb109ca61835f8` |

## Knowledge retrieval evidence

Direct pgvector nearest-neighbor queries against both restored chunk-vector tables returned the expected
saved record at cosine distance **0**. The evidence includes record-ID hashes rather than document contents.

The original LightRAG `/query/data` request returned **HTTP 500** because the saved configuration uses
`text-embedding-3-small` at the external OpenAI host and the sandbox's network deliberately blocks egress.
This was preserved as a failed provider-availability check, not attributed to lost database data.

A separate, internal-only adapter replayed one known embedding from the restored snapshot. LightRAG retained
its recorded model name and **1536** dimensions. With that adapter, the actual `/query/data` endpoint in
`naive` mode returned **HTTP 200**, **3 chunks**, and the expected saved chunk in
**2.322 seconds**. It made **zero paid embedding or generation calls**.
This proves the restored API/storage retrieval path for that reference query; it does not prove live provider
credentials, model inference, generative answering or hybrid/graph query modes.

## Isolation and final state

Actual Docker object inspection confirmed:

- A distinct project and volumes, with **zero overlap with production volume names**.
- The sandbox network `ogr-restore-cd6e6ece9b_default` is **internal**.
- **Zero published host ports**.
- Four retained, healthy database services: PostgreSQL, MongoDB, Activepieces PostgreSQL and Redis.
- Temporary retrieval-test containers were removed. Application writers were not left running in the sandbox.
- All 13 original services were running; the manually paused admin panel was resumed.

The sandbox and encrypted repositories remain available locally for inspection. Their configuration,
password file, extracted restore and detailed run logs are under the ignored, owner-only directory
`/home/krock/Documents/librechat-lightrag-stack/backup/private/live-20261009`. These private artifacts contain recoverable application data and are not included in Git.

## Findings and remaining checks

1. **Untracked writer:** `admin-panel` is running in the local Compose project but is absent from the current
   Compose files. It was manually stopped for capture and restarted afterward. The engine's automatic writer
   inventory does not quiesce this orphan. Before unattended operation, declare that service or reject/quiesce
   unexpected writers.
2. **MCP runtime drift:** the live MCP container differs from current Compose configuration for `LIGHTRAG_URL`,
   `LIGHTRAG_API_KEY` and `MCP_TOKEN`. The backup preserves the declarative configuration, not those runtime-only
   overrides. The restored MCP connection/authentication therefore needs explicit reconciliation. No secret
   values were printed or placed in the evidence report.
3. **Application and provider coverage:** full LibreChat login, MCP integration, workflow execution and live
   external embedding credentials were not exercised. n8n credential decryption and restored storage were exercised.
4. **Operational coverage:** scheduling, missed-backup alerts, remote offsite storage, object locking and historical
   RPO compliance remain commissioning checks. Both repositories used here reside on the same physical host.

## Reproduction

From the repository root, with the retained test configuration and Restic binary:

```sh
PATH=/tmp:$PATH BACKUP_CONFIG=backup/private/live-20261009/backup.env \
  ./scripts/backup/verify.sh e6568d7f98aabc8ae4fe2a07b83dd793345c936108d44fdfa50d38311ad2cb9f

PATH=/tmp:$PATH BACKUP_CONFIG=backup/private/live-20261009/backup.env \
  ./scripts/backup/restore.sh --snapshot e6568d7f98aabc8ae4fe2a07b83dd793345c936108d44fdfa50d38311ad2cb9f --target isolated --cleanup
```

The second command creates and removes a new sandbox; it does not overwrite the retained sandbox or source stack.
The recorded-embedding API adapter was an extra test fixture and is not enabled in the standard restore command.
