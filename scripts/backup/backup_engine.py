#!/usr/bin/env python3
"""Coordinated, encrypted and restore-verified stack backup orchestration."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import signal
import sys
import time
from urllib.parse import quote
import uuid
from pathlib import Path
from typing import Any

from common import Config, checksums, database_evidence, validate_bundle, write_json


class BackupError(RuntimeError):
    pass


RECONSTRUCTIBLE_VOLUMES = {"ollama_data", "librechat_logs", "activepieces_cache"}
DATABASE_SERVICES = {"postgres", "activepieces-postgres"}
WRITER_EXCLUSIONS = DATABASE_SERVICES | {"mongodb", "activepieces-redis", "ollama"}
READ_ONLY_SERVICES = {"mcp", "activepieces-gateway", "ollama-init"}
EXPECTED_CONFIG_FILES = (
    ".env",
    "compose.yaml",
    "compose.activepieces.yaml",
    "compose.local.yaml",
    "compose.mock.yaml",
    "librechat.yaml",
    "postgres/init.sql",
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _stack_revision(c: Config) -> str:
    try:
        return c.run(["git", "rev-parse", "HEAD"]).decode("ascii", "replace").strip()
    except Exception:
        return "unknown"


def _write_status(c: Config, backup_id: str, status: str, **details: Any) -> None:
    record = {"backup_id": backup_id, "status": status, "updated_at": _now(), **details}
    destination = Path(c.state) / "status" / f"{backup_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(destination, record)
    write_json(Path(c.state) / "last-backup.json", {**record, "state": status})


def _config_sources(c: Config, inventory: dict[str, Any]) -> list[tuple[Path, str]]:
    """Collect project config and bind inputs while refusing unhandled external binds."""
    root = Path(c.root).resolve()
    sources: dict[str, Path] = {}
    for rel in EXPECTED_CONFIG_FILES:
        candidate = root / rel
        if candidate.is_file():
            sources[rel] = candidate
    for rel in _compose_file_names(c):
        candidate = root / rel
        if not candidate.is_file():
            raise BackupError(f"Compose file is missing from the project: {rel}")
        sources[rel] = candidate
    for spec in inventory.get("services", {}).values():
        build = spec.get("build")
        if not build:
            continue
        context = Path(build if isinstance(build, str) else build.get("context", "")).resolve()
        try:
            context.relative_to(root)
        except ValueError as error:
            raise BackupError("External build context is unsupported") from error
        for child in context.rglob("*"):
            if child.is_symlink():
                raise BackupError("Build context symlinks are unsupported")
            if child.is_file() and not any(part in {"__pycache__", ".git", ".venv"} for part in child.relative_to(context).parts):
                sources[child.relative_to(root).as_posix()] = child
    services = inventory.get("services", {}) or {}
    for service in services.values():
        for mount in service.get("volumes", []) or []:
            if not isinstance(mount, dict) or mount.get("type") != "bind":
                continue
            source = Path(str(mount.get("source", ""))).resolve()
            try:
                relpath = source.relative_to(root)
            except ValueError as exc:
                raise BackupError("Compose contains a bind mount outside the project; backup refused") from exc
            if source.is_file():
                sources[relpath.as_posix()] = source
            elif source.is_dir():
                for child in sorted(source.rglob("*")):
                    if child.is_symlink():
                        raise BackupError("Bind mount symlinks require an explicit recovery plan")
                    if child.is_file():
                        child_rel = child.relative_to(root).as_posix()
                        sources[child_rel] = child
    return [(path, rel) for rel, path in sorted(sources.items())]


def _compose_file_names(c: Config) -> list[str]:
    root = Path(c.root).resolve()
    names = [name for name in c.env.get("BACKUP_COMPOSE_FILES", "compose.yaml").split(":") if name]
    safe_names = []
    for name in names:
        candidate = (root / name).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise BackupError("Compose file is outside the project; backup refused") from exc
        safe_names.append(relative)
    return safe_names


def _copy_config(c: Config, inventory: dict[str, Any], bundle: Path) -> None:
    # Preserve empty bind directories so the isolated Compose mounts remain valid.
    for specification in inventory.get("services", {}).values():
        for mount in specification.get("volumes", []):
            if mount.get("type") == "bind":
                source = Path(mount["source"]).resolve()
                if source.is_dir():
                    relative = source.relative_to(Path(c.root).resolve())
                    (bundle / "config" / relative).mkdir(parents=True, exist_ok=True)
                    for directory in source.rglob("*"):
                        if directory.is_dir() and not directory.is_symlink():
                            (bundle / "config" / directory.relative_to(c.root)).mkdir(parents=True, exist_ok=True)
    for source, relative in _config_sources(c, inventory):
        destination = bundle / "config" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _database_services(inventory: dict[str, Any], running: set[str]) -> list[str]:
    return [name for name in ("postgres", "activepieces-postgres") if name in running and name in (inventory.get("services") or {})]


def _dump_postgres(c: Config, bundle: Path, service: str) -> list[dict[str, str]]:
    service_env = (c.inventory().get("services", {}).get(service, {}) or {}).get("environment", {}) or {}
    if service == "postgres":
        username = str(service_env.get("POSTGRES_USER") or c.env.get("POSTGRES_USER") or "lightrag")
        default_db = str(service_env.get("POSTGRES_DB") or c.env.get("POSTGRES_DB") or "lightrag")
    else:
        username = str(service_env.get("POSTGRES_USER") or c.env.get("AP_POSTGRES_USERNAME") or "activepieces")
        default_db = str(service_env.get("POSTGRES_DB") or c.env.get("AP_POSTGRES_DATABASE") or "activepieces")
    query = "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname"
    output = c.docker(service, ["psql", "-U", username, "-d", "postgres", "-At", "-c", query])
    databases = [line.strip() for line in output.decode("utf-8", "replace").splitlines() if line.strip()]
    if not databases:
        databases = [default_db]
    records: list[dict[str, str]] = []
    for database in databases:
        safe_database = quote(database, safe="") or "empty"
        relative = f"dumps/{service}/{safe_database}.dump"
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        c.run_to_file([*c.compose, "exec", "-T", service, "pg_dump", "-U", username, "-d", database, "-Fc"], destination)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise BackupError(f"PostgreSQL produced an empty dump for {service}/{database}")
        records.append({"dump_id": str(uuid.uuid4()), "service": service, "kind": "postgres", "database": database, "file": relative})
    return records


def _dump_mongo(c: Config, bundle: Path) -> list[dict[str, str]]:
    relative = "dumps/mongodb/archive.gz"
    destination = bundle / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Credentials are expanded inside the container and never appear in host argv.
    command = (
        'mongodump --username "$MONGO_INITDB_ROOT_USERNAME" '
        '--password "$MONGO_INITDB_ROOT_PASSWORD" '
        '--authenticationDatabase admin --archive --gzip'
    )
    c.run_to_file([*c.compose, "exec", "-T", "mongodb", "sh", "-c", command], destination)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise BackupError("MongoDB produced an empty archive")
    return [{"dump_id": str(uuid.uuid4()), "service": "mongodb", "kind": "mongo", "database": "all", "file": relative}]


def _snapshot_volumes(c: Config, inventory: dict[str, Any], bundle: Path) -> list[str]:
    volumes = inventory.get("volumes", {}) or {}
    captured: list[str] = []
    for logical in sorted(volumes):
        if logical in RECONSTRUCTIBLE_VOLUMES or logical in {
            "postgres_data", "mongo_data", "activepieces_postgres_data"
        }:
            continue
        relative = f"volumes/{logical}.tar"
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        c.archive_volume(logical, destination)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise BackupError(f"Volume snapshot is empty: {logical}")
        captured.append(logical)
    return captured


def _verify_snapshot(c: Config, snapshot_id: str, target: Path, repo: str | None = None) -> None:
    args = ["restore", snapshot_id, "--target", str(target)]
    if repo:
        args.extend(["--repo", repo])
    c.restic(args)
    manifests = list(target.rglob("manifest.json"))
    if len(manifests) != 1:
        raise BackupError("Restored snapshot does not contain exactly one manifest")
    validate_bundle(manifests[0].parent)


def _extract_snapshot_id(output: bytes) -> str:
    # Restic emits one or more JSON records; the summary contains the snapshot id.
    for line in reversed(output.decode("utf-8", "replace").splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("message_type") == "summary" and item.get("snapshot_id"):
            return str(item["snapshot_id"])
    raise BackupError("Restic did not report a snapshot identifier")


def _find_snapshot_id(c: Config, backup_id: str, repo: str | None = None) -> str:
    args = ["snapshots", "--json", "--tag", f"backup-id:{backup_id}"]
    if repo:
        args.extend(["--repo", repo])
    try:
        snapshots = json.loads(c.restic(args))
    except (json.JSONDecodeError, TypeError) as exc:
        raise BackupError("Restic returned an invalid snapshot listing") from exc
    matches = [item.get("id") for item in snapshots if f"backup-id:{backup_id}" in item.get("tags", []) and item.get("id")]
    if len(matches) != 1:
        raise BackupError("Restic snapshot tag did not identify exactly one snapshot")
    return str(matches[0])


def _restic_probe(c: Config, offsite: str | None = None) -> None:
    c.restic(["snapshots", "--json"])
    if offsite:
        c.restic(["snapshots", "--json", "--repo", offsite])


def preflight(c: Config) -> dict[str, Any]:
    inventory = c.inventory()
    if not (Path(c.root) / ".env").is_file():
        raise BackupError("Project .env is required for recovery")
    if not c.env.get("RESTIC_REPOSITORY"):
        raise BackupError("RESTIC_REPOSITORY is required")
    if not c.env.get("RESTIC_PASSWORD_FILE"):
        raise BackupError("RESTIC_PASSWORD_FILE is required")
    services = inventory.get("services", {}) or {}
    running = set(c.running())
    for service in ("postgres", "mongodb"):
        if service not in running:
            raise BackupError(f"Required database service is not running: {service}")
    if "activepieces-postgres" in services:
        for service in ("activepieces-postgres", "activepieces-redis"):
            if service not in running:
                raise BackupError(f"Configured Activepieces service is not running: {service}")
        c.docker("activepieces-redis", ["redis-cli", "PING"])
        ap_env = (services.get("activepieces-postgres", {}) or {}).get("environment", {}) or {}
        ap_user = str(ap_env.get("POSTGRES_USER") or c.env.get("AP_POSTGRES_USERNAME") or "activepieces")
        c.docker("activepieces-postgres", ["pg_isready", "-U", ap_user])
    postgres_env = (services.get("postgres", {}) or {}).get("environment", {}) or {}
    c.docker("postgres", ["pg_isready", "-U", str(postgres_env.get("POSTGRES_USER") or c.env.get("POSTGRES_USER") or "lightrag")])
    c.docker("mongodb", ["mongosh", "--quiet", "--eval", "db.adminCommand({ping:1}).ok"])
    state = Path(c.state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    free = shutil.disk_usage(state).free
    minimum = int(c.env.get("BACKUP_MIN_FREE_BYTES", str(1024 * 1024 * 1024)))
    if free < minimum:
        raise BackupError("Insufficient free space for a backup staging area")
    offsite = c.env.get("BACKUP_OFFSITE_REPOSITORY")
    if not offsite:
        raise BackupError("BACKUP_OFFSITE_REPOSITORY is required")
    if offsite == c.env.get("RESTIC_REPOSITORY"):
        raise BackupError("Offsite repository must differ from the primary repository")
    _restic_probe(c, offsite)
    primary_config = json.loads(c.restic(["cat", "config"]))
    offsite_config = json.loads(c.restic(["cat", "config", "--repo", offsite]))
    if primary_config.get("id") == offsite_config.get("id"):
        raise BackupError("Primary and offsite must be independently initialized repositories")
    _config_sources(c, inventory)
    return {"running": sorted(running), "services": sorted(services), "free_bytes": free}


def backup(c: Config, *, before_upgrade: bool = False) -> str:
    # Hold the global lock through both backup execution and writer recovery.
    with c.lock():
        return _backup_body(c, before_upgrade=before_upgrade)


def _backup_body(c: Config, *, before_upgrade: bool = False) -> str:
    start = time.monotonic()
    backup_id = str(uuid.uuid4())
    state = Path(c.state)
    staging_root = state / "staging"
    staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = staging_root / backup_id
    # Snapshots are not eligible for retention or restore selection until verified.
    tag_args = ["--tag", f"backup-id:{backup_id}", "--tag", "unverified"]
    if before_upgrade:
        tag_args += ["--tag", "before-upgrade"]
    stopped: list[str] = []
    redis_stopped: list[str] = []
    snapshot_id: str | None = None
    offsite_snapshot_id: str | None = None
    backup_verified = False
    _write_status(c, backup_id, "running", started_at=_now())

    def stop_service(name: str) -> None:
        stopped.append(name)
        c.run([*c.compose, "stop", name])

    try:
        details = preflight(c)
        inventory = c.inventory()
        running = set(details["running"])
        services = inventory.get("services", {}) or {}
        writers = []
        for name in services:
            if name not in running or name in WRITER_EXCLUSIONS | READ_ONLY_SERVICES:
                continue
            writers.append(name)
        stage.mkdir(parents=True, mode=0o700)
        for service in sorted(writers):
            stop_service(service)
        # Activepieces Redis is persistent and must be frozen while its volume is archived.
        if "activepieces-redis" in running:
            if c.docker("activepieces-redis", ["redis-cli", "SAVE"]).strip() != b"OK":
                raise BackupError("Activepieces Redis persistence failed")
            redis_stopped.append("activepieces-redis")
            c.run([*c.compose, "stop", "activepieces-redis"])

        dump_records: list[dict[str, str]] = []
        for service in _database_services(inventory, running):
            dump_records.extend(_dump_postgres(c, stage, service))
        dump_records.extend(_dump_mongo(c, stage))
        captured_volumes = _snapshot_volumes(c, inventory, stage)
        _copy_config(c, inventory, stage)

        project = inventory
        resolved_config = stage / "config" / "compose.resolved.json"
        resolved_config.parent.mkdir(parents=True, exist_ok=True)
        write_json(resolved_config, project)
        config = {
            "schema_version": 1,
            "backup_id": backup_id,
            "captured_at": _now(),
            "stack_revision": _stack_revision(c),
            "project_root": str(Path(c.root).resolve()),
            "compose_files": _compose_file_names(c),
            "restore_compose_file": "config/compose.resolved.json",
            "workspace": str(((project.get("services", {}).get("lightrag", {}) or {}).get("environment", {}) or {}).get("WORKSPACE") or c.env.get("LIGHTRAG_WORKSPACE", "company_bge_m3")),
            "embedding": {
                "model": str(((project.get("services", {}).get("lightrag", {}) or {}).get("environment", {}) or {}).get("EMBEDDING_MODEL") or c.env.get("EMBEDDING_MODEL", "bge-m3")),
                "dimension": str(((project.get("services", {}).get("lightrag", {}) or {}).get("environment", {}) or {}).get("EMBEDDING_DIM") or c.env.get("EMBEDDING_DIM", "1024")),
            },
            "images": {name: str(svc.get("image", "")) for name, svc in (project.get("services", {}) or {}).items()},
            "volumes": captured_volumes,
            "dumps": dump_records,
            "database_evidence": database_evidence(c),
        }
        config["checksums"] = checksums(stage)
        config["state"] = "complete"
        write_json(stage / "manifest.json", config)
        validate_bundle(stage)

        output = c.restic(["backup", *tag_args, "--json", str(stage)])
        snapshot_id = _extract_snapshot_id(output)

        primary_verify = state / "verify-primary" / backup_id
        primary_verify.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _verify_snapshot(c, snapshot_id, primary_verify)
        shutil.rmtree(primary_verify, ignore_errors=True)

        offsite = c.env.get("BACKUP_OFFSITE_REPOSITORY")
        if offsite:
            c.restic(["copy", "--from-repo", c.env["RESTIC_REPOSITORY"], "--from-password-file", c.env["RESTIC_PASSWORD_FILE"], "--repo", offsite, snapshot_id])
            offsite_id = _find_snapshot_id(c, backup_id, offsite)
            verify_offsite = state / "verify-offsite" / backup_id
            verify_offsite.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _verify_snapshot(c, offsite_id, verify_offsite, offsite)
            shutil.rmtree(verify_offsite, ignore_errors=True)
        else:
            raise BackupError("Offsite repository is required for verification")

        if before_upgrade:
            primary_tags = ["tag", "--remove", "unverified", snapshot_id]
        else:
            primary_tags = ["tag", "--remove", "unverified", "--add", "ogr-backup", snapshot_id]
        c.restic(primary_tags)
        snapshot_id = _find_snapshot_id(c, backup_id)
        if offsite:
            if before_upgrade:
                c.restic(["tag", "--repo", offsite, "--remove", "unverified", offsite_id])
            else:
                c.restic(["tag", "--repo", offsite, "--remove", "unverified", "--add", "ogr-backup", offsite_id])
            offsite_id = _find_snapshot_id(c, backup_id, offsite)
            offsite_snapshot_id = offsite_id

        _apply_retention(c, offsite)
        backup_verified = True
        return backup_id
    except BaseException as exc:
        safe_error = str(exc) if isinstance(exc, (BackupError, RuntimeError)) else type(exc).__name__
        _write_status(c, backup_id, "unverified" if snapshot_id else "failed", snapshot_id=snapshot_id, error=safe_error[:500], elapsed_seconds=round(time.monotonic() - start, 3), finished_at=_now())
        raise
    finally:
        resume_errors = []
        for service in redis_stopped + list(reversed(stopped)):
            try:
                c.run([*c.compose, "up", "-d", "--no-deps", "--no-recreate", "--wait", "--wait-timeout", "180", service])
            except Exception as exc:  # Keep trying to resume every writer.
                resume_errors.append(type(exc).__name__)
        for clear_path in (stage, state / "verify-primary" / backup_id, state / "verify-offsite" / backup_id):
            shutil.rmtree(clear_path, ignore_errors=True)
        if resume_errors:
            _write_status(c, backup_id, "unverified", snapshot_id=snapshot_id, resume_errors=resume_errors, finished_at=_now())
            raise BackupError("Backup could not resume all stopped services")
        if backup_verified and snapshot_id:
            _write_status(c, backup_id, "complete", snapshot_id=snapshot_id, offsite_snapshot_id=offsite_snapshot_id, elapsed_seconds=round(time.monotonic() - start, 3), finished_at=_now())


def _apply_retention(c: Config, offsite: str | None) -> None:
    # Restic retention is restricted to managed snapshots. Upgrade checkpoints
    # carry a separate tag and are excluded from this policy by using the exact
    # managed tag on ordinary backups only.
    if offsite:
        c.restic(["forget", "--repo", offsite, "--group-by", "", "--tag", "ogr-backup", "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6", "--prune"])
    c.restic(["forget", "--group-by", "", "--tag", "ogr-backup", "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6", "--prune"])


def snapshot_volumes(c: Config, destination: Path) -> list[str]:
    inventory = c.inventory()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    return _snapshot_volumes(c, inventory, destination)


def main(c: Config, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    snapshot_parser = subparsers.add_parser("snapshot-volumes")
    snapshot_parser.add_argument("--destination", required=True, type=Path)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--before-upgrade", action="store_true")
    args = parser.parse_args(argv)

    def interrupted(signum: int, _frame: Any) -> None:
        raise BackupError(f"Interrupted by signal {signum}")

    old_int = signal.signal(signal.SIGINT, interrupted)
    old_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.command == "preflight":
            preflight(c)
            print("Backup preflight passed")
        elif args.command == "snapshot-volumes":
            for name in snapshot_volumes(c, args.destination):
                print(name)
        else:
            backup_id = backup(c, before_upgrade=args.before_upgrade)
            print(backup_id)
        return 0
    except Exception as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


if __name__ == "__main__":
    raise SystemExit(main(Config(), sys.argv[1:]))
