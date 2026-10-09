# Disaster recovery

This guide covers loss of the host, application data corruption, and loss of
the local Restic repository. It assumes the offsite repository and secret
escrow are stored separately from the application host.

## Recovery targets

- **RPO:** 24 hours, based on daily backups. Confirm the actual interval from
  the latest verified snapshot and the incident time.
- **RTO:** two hours for the reference dataset. This is a target, not a measured
  guarantee; use weekly restore drill timings to assess it.
- **Recovery proof:** a successful isolated restore and deterministic checks of
  database records and original files. Model-generated output is not a recovery
  test.

## Host or local repository loss

1. Provision a clean host and install Docker Compose and Restic.
2. Retrieve the Restic password, offsite repository credentials, and application
   secrets from their independent escrow. The backup host alone must not be the
   only copy of these recovery materials.
3. Check out the version recorded in the backup manifest and create the
   protected backup environment file. Point it to the offsite repository.
4. List snapshots and select the newest verified one that predates the failure.
5. Run and review an isolated restore using the
   [restore runbook](restore-runbook.md).
6. After the isolated checks pass, restore onto a clean production target using
   a separately reviewed promotion procedure. The automated restore command
   does not overwrite the live stack.
7. Start dependencies and applications in order: databases, LightRAG and its
   files, workflow service, bridge, then LibreChat. Confirm health, user login,
   workflow availability, and a deterministic knowledge query before reopening
   normal use.
8. Record incident time, selected snapshot, recovery start/end, validation
   results, and any lost changes after the snapshot.

## Data corruption or bad upgrade

Stop writes to the affected services, preserve the current volumes for analysis,
and identify the last verified snapshot before corruption. Test it in isolation.
For an upgrade failure, use the snapshot tagged `before-upgrade`, together with
the exact image versions in its manifest. Do not delete the failed state until
recovery is accepted and evidence has been retained.

## Backup and restore failure response

Systemd service failures trigger the configured failure unit. The independent
hourly monitor detects a missing successful backup even if the service never
ran. Check the systemd journal, state directory status, host disk space,
repository connectivity, and credentials. Fix the cause, run a manual backup,
then complete an isolated restore before declaring the recovery chain healthy.

## Recovery readiness

Review readiness at least quarterly and after changing Compose services,
volumes, image versions, repository credentials, or retention rules. Confirm
that the offsite repository can be read from a clean host and that escrowed
secrets still work. A repository that has not been restored is not a proven
recovery path.
