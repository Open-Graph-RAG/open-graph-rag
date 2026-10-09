# Backup policy

This policy protects the Open Graph RAG stack against host loss, operator
error, and accidental corruption. The initial recovery point objective is 24
hours, based on one daily backup. The target recovery time is two hours; it is
an objective until a restore drill measures it on the reference dataset.

## Protected data

Backups include database-aware dumps and the related application files:

| Data | Source | Recovery importance |
|---|---|---|
| LightRAG graph, chunks, vectors and document status | PostgreSQL logical dump | Critical |
| LibreChat users and conversations | MongoDB archive | Critical |
| Original LightRAG documents and application state | LightRAG file volumes | Critical |
| LibreChat uploads, images and data | LibreChat file volumes | High |
| n8n workflows and credentials | n8n volume | Critical |
| Compose files and application configuration | Repository snapshot | Critical |
| Activepieces state, when enabled | Overlay volumes and configuration | Critical |
| Ollama model cache | `ollama_data` volume | Rebuildable; included if configured |

Never copy a running PostgreSQL or MongoDB volume as a substitute for its
logical dump. The database dump and file snapshot are coordinated by briefly
quiescing application writers, because these stores do not share a transaction.
The backup wrapper must restart paused writers on both success and failure.

## Schedule and verification

Run a full encrypted backup daily. Each backup is verified in both repositories
before it receives the managed `ogr-backup` tag or enters retention. Replicate every successful backup to a
separate offsite Restic repository. A weekly restore drill extracts the latest
snapshot into an isolated stack and checks the restored services and fixture
data. An hourly monitor alerts when no recent successful backup is recorded.

Systemd runs backup and restore jobs outside the application stack. Service
failures go through the configured `OnFailure` unit and are recorded in the
journal; the independent monitor detects missed runs even when a timer or host
unit was disabled. Forward the `ogr-backup` journal tag to the host's alerting
system for off-host notification; neither the service nor its logs include
repository passwords.

## Retention and protected snapshots

Apply [`backup/policies/retention.yaml`](../../backup/policies/retention.yaml)
only to snapshots carrying the managed tag, which is applied after verification:

- Keep 7 daily, 4 weekly, and 6 monthly snapshots.
- Keep at least one verified snapshot at all times.
- Preserve snapshots tagged `before-upgrade` until an operator removes that
  tag after validating the upgrade and rollback window.
- Never prune an unverified snapshot as though it were a valid recovery point.
- Apply the same retention to the offsite copy.

Restic encryption protects confidentiality but does not make a repository
immutable. For ransomware protection, use a separate offsite identity with
write-only backup access and object-lock or equivalent retention controls.

## Secrets and access

Store the backup environment file, Restic password, object-store credentials,
and any application secrets outside Git. Restrict them to the backup operator
and root. Keep a tested recovery copy of the Restic password and object-store
access instructions off-host. Never send production repositories or credentials
to public CI; CI uses generated fixture data and disposable repositories only.

## Operations

Run `scripts/backup/preflight.sh` before the first scheduled run and after
changing repositories or Compose files. Inspect the latest backup and monitor
state after any failure. Do not treat command success alone as proof of a
usable recovery point; require snapshot verification and the weekly isolated
restore. Record drill duration to track the two-hour RTO objective.

## Validation record

On 2026-10-09, the final synthetic Docker/Restic end-to-end test passed in
111.830 seconds total using Restic 0.18.1, PostgreSQL 17 with pgvector,
MongoDB 8, Activepieces PostgreSQL 14, and Redis 7.0.7. The isolated restore
phase took 32.867 seconds. The test compared table and collection fingerprints,
original documents and encrypted credential markers, Redis contents, and direct
pgvector nearest-neighbor, chunk, and graph-edge queries. It also rejected
corrupted dumps, wrong repository credentials and an unavailable offsite target,
and confirmed isolated volume cleanup.

These timings describe a small synthetic fixture, not a production reference
dataset or a LightRAG application API query. The offsite target was a second
local Restic repository. Production RTO, S3 credentials and object-lock behavior,
application login/credential decryption, local model reconstruction, and external
alert forwarding remain host commissioning checks. The implementation supplies
the units; they have not been installed or enabled on the production host.
