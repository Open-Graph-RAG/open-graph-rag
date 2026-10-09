# Restore runbook

Use the isolated restore to validate a recovery point without affecting the
running stack. The commands below assume the backup environment is loaded by
systemd or exported in the shell and that Docker and Restic are available.

## Isolated verification restore

1. Check available disk space and confirm the local and offsite repositories
   are reachable. Use the offsite repository if the host or local repository is
   suspect.
2. List and inspect snapshots. Select a snapshot explicitly for an incident;
   use `latest` for the scheduled drill.
3. Run the restore in an isolated target:

   ```sh
   BACKUP_CONFIG=/etc/open-graph-rag/backup.env \
     ./scripts/backup/restore.sh --snapshot latest --target isolated --cleanup
   ```

4. Wait for the command's validation report. It must confirm PostgreSQL and
   MongoDB restore, fixture/document files, expected graph records, and
   application data. Preserve the report with the incident or drill record.
5. Confirm cleanup removed only the temporary restore project and volumes.
   Production services and volumes must remain untouched.

The weekly systemd timer runs the same isolated operation. A failed restore is
an alert and must be investigated before relying on that recovery point.

## Restore after host or data loss

1. Contain the incident and preserve any surviving disks or repositories for
   investigation. Do not run `docker compose down -v` against the original
   project.
2. Rebuild a clean host, install Docker Compose and Restic, and check out the
   exact application version recorded by the selected snapshot manifest.
3. Restore the backup configuration and secrets from the independent escrow.
   Configure `RESTIC_REPOSITORY` to the recovery source and verify repository
   access with the supplied password file.
4. Validate the candidate recovery point in isolation first. Choose the newest
   verified snapshot at or before the incident; avoid a snapshot that fails
   integrity or application checks.
5. Restore the production stack only after the isolated check passes and a
   separate recovery target has been prepared. The current automated interface
   intentionally exposes only the isolated target; do not point it at live
   volumes or networks.
6. Follow the disaster recovery guide to bring the stack online in dependency
   order, validate user access and known knowledge queries, then record the
   recovery start/end times and data loss window.

## Corrupt or unavailable snapshot

Select the prior verified snapshot and repeat the isolated restore. If the
local repository is unavailable, use the independently configured offsite
repository. Keep failed artifacts and logs for diagnosis; do not mark a failed
snapshot as verified or prune the last known-good recovery point. Escalate
repository credentials, storage health, and backup schedule failures to the
operator responsible for the backup host.
