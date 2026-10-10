# Backup operations

The backup subsystem uses logical PostgreSQL and MongoDB dumps, snapshots the
application files and configuration, then stores the result in encrypted
Restic repositories. Configure a local repository and a separate offsite
repository before scheduling it. The local and offsite locations must not
resolve to the same repository.

Copy [`config.example.env`](config.example.env) to
`/etc/open-graph-rag/backup.env` on the host and set its values there. Keep the
Restic password file and repository credentials outside Git and outside the
backup state directory. Protect the password file with owner-only permissions
and keep an independent recovery copy: a Restic repository cannot be opened
without it.

`BACKUP_COMPOSE_FILES` is a colon-separated list of Compose files, each relative
to the repository root. The default is `compose.yaml`. Include overlays used by the
running stack, for example `compose.yaml:compose.activepieces.yaml`. The backup
commands use this same project and its existing named volumes. `BACKUP_STATE_DIR`
contains temporary dumps, lock and status data; it is private host state and
must have enough free space for a complete database dump.

The repository supplies systemd units but does not install or enable them on a
host. After configuring storage and secrets, an operator installs the units
from `deploy/systemd/` and enables the daily backup, weekly isolated restore
and hourly missed-backup monitor timers. See the
[backup policy](../docs/operations/backup-policy.md),
[restore runbook](../docs/operations/restore-runbook.md), and
[disaster recovery guide](../docs/operations/disaster-recovery.md).

For a host installed at `/opt/open-graph-rag`, install the protected
configuration and units as root:

```sh
install -d -m 0700 /etc/open-graph-rag
install -d -m 0700 /opt/open-graph-rag/.backup-state
install -d -m 0700 /srv/open-graph-rag-restic
install -m 0600 backup/config.example.env /etc/open-graph-rag/backup.env
umask 077; openssl rand -hex 32 > /etc/open-graph-rag/restic-password
install -m 0644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
```

Edit `backup.env` and initialize both repositories before enabling timers. Use
the configured offsite URL in place of the placeholder below:

```sh
RESTIC_REPOSITORY=/srv/open-graph-rag-restic \
  RESTIC_PASSWORD_FILE=/etc/open-graph-rag/restic-password restic init
RESTIC_PASSWORD_FILE=/etc/open-graph-rag/restic-password \
  restic -r '<BACKUP_OFFSITE_REPOSITORY value>' init
systemctl enable --now ogr-backup.timer ogr-restore-test.timer ogr-backup-monitor.timer
```

The local and offsite repositories use the same Restic password. Verify both
can be opened from a separate recovery host before relying on them. The services pass `backup.env` through
`BACKUP_CONFIG`, whose parser reads dotenv assignments without executing shell
code. Save an independent recovery copy of
the generated Restic password. For a checkout
at a different path, add a systemd drop-in override for `WorkingDirectory`,
`BACKUP_CONFIG`, and the absolute `ExecStart` path in each relevant service.
The Compose file list remains relative to the repository root. If the primary
Restic repository uses a different host path, add it to `ReadWritePaths` in the
`ogr-backup.service` drop-in as well.

For manual operation from the repository root:

```sh
BACKUP_CONFIG=/etc/open-graph-rag/backup.env ./scripts/backup/preflight.sh
BACKUP_CONFIG=/etc/open-graph-rag/backup.env ./scripts/backup/backup.sh
BACKUP_CONFIG=/etc/open-graph-rag/backup.env ./scripts/backup/restore.sh --snapshot latest --target isolated --cleanup
```

The restore command creates an isolated Compose project and uses separate
networks and volumes. Never promote a verification restore by attaching it to
the production project. A successful scheduled backup is not proof of recovery;
the weekly isolated restore is the recovery check.
