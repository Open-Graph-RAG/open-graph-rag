#!/usr/bin/env python3
"""Restore and verify a backup in a uniquely named, isolated Compose project."""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import signal
import shutil
import sys
import tempfile
import time
import hashlib
import tarfile
import uuid
from pathlib import Path
from typing import Any

try:
    from . import common
except ImportError:  # Direct execution from the backup scripts directory.
    import common


SNAPSHOT_ID = re.compile(r"^[A-Fa-f0-9]{8,64}$")
DATABASE_VOLUMES = {"postgres_data", "mongo_data", "activepieces_postgres_data"}
REQUIRED_DUMPS = {"postgres", "mongo"}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Restic snapshot ID or latest")
    parser.add_argument("--target", choices=["isolated"])
    parser.add_argument("--verify-only", action="store_true", help="Verify snapshot contents without starting containers")
    parser.add_argument("--workspace", help="Expected LightRAG workspace")
    parser.add_argument("--embedding-model", help="Expected embedding model")
    parser.add_argument("--embedding-dim", type=int, help="Expected embedding dimension")
    parser.add_argument("--start-apps", action="store_true", help="Start non-database services after data checks")
    parser.add_argument("--query-context", help="Optional deterministic LightRAG context query")
    parser.add_argument("--expect-context", help="Optional text that must appear in the retrieved context")
    parser.add_argument("--cleanup", action="store_true", help="Remove the isolated stack and restore files")
    args = parser.parse_args(argv)
    if not args.verify_only and args.target != "isolated":
        parser.error("--target isolated is required for restore")
    if args.verify_only and (args.start_apps or args.query_context or args.expect_context or args.cleanup):
        parser.error("--verify-only cannot be combined with restore options")
    if args.expect_context and not args.query_context:
        parser.error("--expect-context requires --query-context")
    return args


def _resolve_snapshot(c: Any, requested: str) -> str:
    if requested == "latest":
        raw = c.restic(["snapshots", "--json", "--tag", "ogr-backup"])
        try:
            snapshots = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Restic returned invalid snapshot metadata") from exc
        if not isinstance(snapshots, list) or not snapshots:
            raise RuntimeError("No complete backup snapshots are available")
        candidates = [item for item in snapshots if isinstance(item, dict) and item.get("id")]
        if not candidates:
            raise RuntimeError("No usable backup snapshot IDs were returned")
        candidates.sort(key=lambda item: (item.get("time", ""), item["id"]))
        return str(candidates[-1]["id"])
    if not SNAPSHOT_ID.fullmatch(requested):
        raise RuntimeError("Snapshot must be a hexadecimal Restic ID or 'latest'")
    return requested


def _find_bundle(restored: Path) -> Path:
    manifests = list(restored.rglob("manifest.json"))
    if len(manifests) != 1:
        raise RuntimeError(f"Expected exactly one manifest.json in restored snapshot; found {len(manifests)}")
    return manifests[0].parent


def _read_saved_env(config_dir: Path) -> dict[str, str]:
    path = config_dir / ".env"
    if not path.is_file():
        raise RuntimeError("Backup does not contain config/.env")
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def _check_compatibility(manifest: dict[str, Any], saved_env: dict[str, str], args: argparse.Namespace, resolved: dict[str, Any] | None = None) -> None:
    embedding = manifest.get("embedding") or {}
    checks = {
        "workspace": (args.workspace, manifest.get("workspace")),
        "embedding model": (args.embedding_model, embedding.get("model")),
        "embedding dimension": (args.embedding_dim, embedding.get("dimension")),
    }
    for label, (expected, actual) in checks.items():
        if expected is not None and str(expected) != str(actual):
            raise RuntimeError(f"Backup {label} mismatch: expected {expected!r}, found {actual!r}")
    service_env = (((resolved or {}).get("services") or {}).get("lightrag") or {}).get("environment") or {}
    config_values = {
        "workspace": service_env.get("WORKSPACE", saved_env.get("LIGHTRAG_WORKSPACE")),
        "embedding model": service_env.get("EMBEDDING_MODEL", saved_env.get("EMBEDDING_MODEL")),
        "embedding dimension": service_env.get("EMBEDDING_DIM", saved_env.get("EMBEDDING_DIM")),
    }
    manifest_values = {
        "workspace": manifest.get("workspace"),
        "embedding model": embedding.get("model"),
        "embedding dimension": embedding.get("dimension"),
    }
    for label, value in config_values.items():
        if value is not None and str(value) != str(manifest_values[label]):
            raise RuntimeError(f"Recorded effective configuration {label} does not match the backup manifest")


def _load_resolved_compose(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    path_value = manifest.get("restore_compose_file")
    if not path_value:
        return None
    rel = Path(path_value)
    if rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError("Resolved Compose path is unsafe")
    path = bundle / rel
    try:
        path.resolve().relative_to(bundle.resolve())
        compose = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Saved resolved Compose configuration is invalid") from exc
    if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict):
        raise RuntimeError("Saved resolved Compose configuration has no services")
    return compose


def _compose_files(config_dir: Path, manifest: dict[str, Any], env: dict[str, str]) -> list[Path]:
    saved_files = manifest.get("compose_files")
    saved = ":".join(saved_files) if isinstance(saved_files, list) else env.get("BACKUP_COMPOSE_FILES", "compose.yaml")
    files: list[Path] = []
    for item in saved.split(":"):
        rel = Path(item)
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError("Saved Compose file path is unsafe")
        path = config_dir / rel
        if not path.is_file():
            raise RuntimeError(f"Saved Compose file is missing: {item}")
        files.append(path)
    return files


def _sandbox_env(saved: dict[str, str], project: str, state_dir: Path, transport: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(saved)
    env.pop("BACKUP_CONFIG", None)
    for key in list(env):
        if key.startswith("RESTIC_") or (key.startswith("BACKUP_") and key not in {"BACKUP_COMPOSE_FILES"}):
            env.pop(key, None)
    env["COMPOSE_PROJECT_NAME"] = project
    env["BACKUP_COMPOSE_FILES"] = "compose.yaml"
    env["BACKUP_STATE_DIR"] = str(state_dir)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for key in ("HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        if transport and key in transport:
            env[key] = transport[key]
    # Avoid leaking production-facing endpoint configuration into the restore stack.
    for key in ("KNOWLEDGE_API_KEY", "CHAT_API_KEY", "OPENAI_API_KEY", "EMBEDDING_API_KEY"):
        env.pop(key, None)
    return env


def _render_sandbox_compose(c: Any, config_dir: Path, bundle: Path, env: dict[str, str], target: Path, manifest: dict[str, Any]) -> Path:
    _compose_files(config_dir, manifest, env)
    resolved_compose = _load_resolved_compose(bundle, manifest)
    if resolved_compose is not None:
        compose = resolved_compose
        project_root = manifest.get("project_root")
        if not project_root:
            raise RuntimeError("Backup manifest is missing the source project root needed for bind mounts")
        source_root = Path(project_root).resolve()
    else:
        render_env = dict(env)
        render_env["BACKUP_STATE_DIR"] = str(target / ".render-state")
        render_env["BACKUP_COMPOSE_FILES"] = ":".join(manifest.get("compose_files", ["compose.yaml"]))
        saved_config = common.Config(root=config_dir, env=render_env)
        cmd = ["docker", "compose", "--project-directory", str(config_dir), "--env-file", str(config_dir / ".env")]
        for compose_file in _compose_files(config_dir, manifest, env):
            cmd.extend(["-f", str(compose_file)])
        cmd.extend(["config", "--format", "json"])
        raw = saved_config.run(cmd)
        try:
            compose = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Saved Compose configuration could not be expanded") from exc
        source_root = config_dir
    services = compose.get("services") or {}
    expected_images = manifest.get("images") or {}
    actual_images = {name: str(service.get("image", "")) for name, service in services.items()}
    if {name: str(image) for name, image in expected_images.items()} != actual_images:
        raise RuntimeError("Saved Compose service images do not match the backup manifest")
    if not {"postgres", "mongodb"} <= set(services):
        raise RuntimeError("Saved Compose configuration is missing a required database service")
    selected: dict[str, Any] = {}
    volume_sources: set[str] = set(manifest.get("volumes", [])) | DATABASE_VOLUMES
    for name, original in services.items():
        if name in {"ollama-init"}:
            continue
        service = json.loads(json.dumps(original))
        service.pop("ports", None)
        service.pop("restart", None)
        service.pop("container_name", None)
        service.pop("network_mode", None)
        service.pop("env_file", None)
        aliases: set[str] = set()
        original_networks = service.get("networks") or {}
        if isinstance(original_networks, dict):
            for options in original_networks.values():
                if isinstance(options, dict):
                    aliases.update(str(alias) for alias in options.get("aliases", []))
        service["networks"] = {"default": {"aliases": sorted(aliases)}} if aliases else ["default"]
        build = service.get("build")
        if isinstance(build, (str, dict)):
            build_config = {"context": build} if isinstance(build, str) else build
            context = Path(str(build_config.get("context", ""))).resolve()
            try:
                relative_context = context.relative_to(source_root)
            except ValueError as exc:
                raise RuntimeError(f"Saved build context for {name} is outside the project") from exc
            replacement_context = config_dir / relative_context
            if not replacement_context.is_dir():
                raise RuntimeError(f"Saved build context is missing from backup: {relative_context.as_posix()}")
            build_config["context"] = str(replacement_context.resolve())
            service["build"] = build_config
        else:
            service.pop("build", None)
        if not service.get("image") and not service.get("build"):
            continue
        environment = service.get("environment")
        if isinstance(environment, dict):
            for key in environment:
                if key.endswith("_API_KEY") and key not in {"LIGHTRAG_API_KEY", "MCP_TOKEN"}:
                    environment[key] = "restore-validation-disabled"
        service["volumes"] = list(service.get("volumes", []))
        safe_mounts = []
        for mount in service["volumes"]:
            if isinstance(mount, dict) and mount.get("type") == "volume":
                volume_sources.add(str(mount.get("source", "")))
            elif isinstance(mount, dict) and mount.get("type") == "bind":
                source = Path(str(mount.get("source", ""))).resolve()
                try:
                    relative = source.relative_to(source_root)
                except ValueError as exc:
                    raise RuntimeError("Saved Compose bind mount points outside the saved project") from exc
                replacement = config_dir / relative
                if not replacement.exists():
                    raise RuntimeError(f"Saved bind mount is missing from backup: {relative.as_posix()}")
                mount["source"] = str(replacement.resolve())
            safe_mounts.append(mount)
        service["volumes"] = safe_mounts
        if name in {"postgres", "mongodb", "activepieces-postgres"}:
            service["volumes"].append({"type": "bind", "source": str(bundle.resolve()), "target": "/ogr-restore", "read_only": True})
        depends = service.get("depends_on")
        selected[name] = service
    for service in selected.values():
        depends = service.get("depends_on")
        if isinstance(depends, dict):
            service["depends_on"] = {dep: value for dep, value in depends.items() if dep in selected}
            if not service["depends_on"]:
                service.pop("depends_on")
        elif isinstance(depends, list):
            service["depends_on"] = [dep for dep in depends if dep in selected]
            if not service["depends_on"]:
                service.pop("depends_on")
    clean = {
        "name": env["COMPOSE_PROJECT_NAME"],
        "services": selected,
        "volumes": {name: {} for name in sorted(v for v in volume_sources if v)},
        "networks": {"default": {"internal": True}},
    }
    path = target / "compose.restore.json"
    path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return path


def _compose(c: Any, env: dict[str, str], file: Path, args: list[str]) -> bytes:
    return c.run(["docker", "compose", "--project-name", env["COMPOSE_PROJECT_NAME"], "-f", str(file), *args])


def _archive_fingerprint(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    with tarfile.open(path, "r:*") as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./").rstrip("/")
            if not name:
                continue
            record: dict[str, Any] = {
                "mode": member.mode & 0o7777,
                "uid": member.uid,
                "gid": member.gid,
                "type": "file" if member.isfile() else "directory" if member.isdir() else "symlink" if member.issym() else "hardlink" if member.islnk() else "other",
            }
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError("Unable to read a restored volume file")
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                record["size"] = member.size
                record["sha256"] = digest.hexdigest()
            elif member.issym() or member.islnk():
                record["link"] = member.linkname
            result[name] = record
    return result


def _restore_volume_files(c: Any, bundle: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for logical in manifest.get("volumes", []):
        if logical in DATABASE_VOLUMES:
            # Databases are restored from logical dumps to preserve database-level consistency.
            continue
        archive = bundle / "volumes" / f"{logical}.tar"
        if not archive.is_file():
            raise RuntimeError(f"Missing volume archive for {logical}")
        c.restore_volume(logical, archive)
        roundtrip = Path(c.state) / f"restored-{logical}-{uuid.uuid4().hex}.tar"
        try:
            c.archive_volume(logical, roundtrip)
            expected = _archive_fingerprint(archive)
            actual = _archive_fingerprint(roundtrip)
            if actual != expected:
                raise RuntimeError(f"Restored volume content mismatch: {logical}")
            evidence[logical] = {"entries": len(actual), "sha256": hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()}
        finally:
            roundtrip.unlink(missing_ok=True)
    return evidence


def _restore_databases(c: Any, env: dict[str, str], compose_file: Path, bundle: Path, manifest: dict[str, Any]) -> None:
    dumps = manifest.get("dumps", [])
    allowed_pairs = {("postgres", "postgres"), ("activepieces-postgres", "postgres"), ("mongodb", "mongo")}
    for item in dumps:
        if not isinstance(item, dict) or (item.get("service"), item.get("kind")) not in allowed_pairs:
            raise RuntimeError("Database dump service/kind is unsupported")
        if item.get("kind") == "postgres" and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_$-]{0,62}", str(item.get("database", ""))):
            raise RuntimeError("PostgreSQL database name is invalid")
        if item.get("kind") == "mongo" and item.get("database") != "all" and not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", str(item.get("database", ""))):
            raise RuntimeError("MongoDB database name is invalid")
    kinds = {item.get("kind") for item in dumps if isinstance(item, dict)}
    if not REQUIRED_DUMPS <= kinds:
        raise RuntimeError("Backup manifest is missing PostgreSQL or MongoDB logical dumps")
    if "activepieces-postgres" in (manifest.get("images") or {}) and not any(item.get("service") == "activepieces-postgres" for item in dumps):
        raise RuntimeError("Activepieces is enabled but its PostgreSQL dump is missing")
    database_services = {item.get("service") for item in dumps}
    database_services.update({"postgres", "mongodb"})
    if any(item.get("service") == "activepieces-postgres" for item in dumps):
        database_services.add("activepieces-postgres")
        if "activepieces-redis" in (manifest.get("images") or {}):
            database_services.add("activepieces-redis")
    _compose(c, env, compose_file, ["up", "-d", "--wait", "--wait-timeout", "180", *sorted(database_services)])
    service_specs = json.loads(compose_file.read_text(encoding="utf-8"))["services"]
    for item in dumps:
        kind, db, rel = item["kind"], item["database"], item["file"]
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("Database dump path is unsafe")
        container_file = "/ogr-restore/" + path.as_posix()
        if kind == "postgres":
            service = item["service"]
            service_env = service_specs[service].get("environment", {})
            user = str(service_env.get("POSTGRES_USER", "activepieces" if service == "activepieces-postgres" else "lightrag"))
            default_db = str(service_env.get("POSTGRES_DB", "activepieces" if service == "activepieces-postgres" else "lightrag"))
            if db not in {"postgres", default_db}:
                # The dump may contain databases beyond the image's initialized default.
                exists = c.docker(service, ["psql", "-X", "-A", "-t", "-U", user, "-d", "postgres", "-c", "SELECT 1 FROM pg_database WHERE datname = '" + db.replace("'", "''") + "'"]).decode().strip()
                if exists != "1":
                    c.docker(service, ["createdb", "-U", user, db])
            result = c.docker(service, ["pg_restore", "--clean", "--if-exists", "--no-owner", "--exit-on-error", "-U", user, "-d", db, container_file])
        elif kind == "mongo":
            mongo_command = 'mongorestore --gzip --archive="$1" --drop --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin'
            if db != "all":
                mongo_command += ' --nsInclude "$2.*"'
            mongo_command += ' --nsExclude "admin.*" --nsExclude "local.*" --nsExclude "config.*"'
            mongo_args = ["sh", "-c", mongo_command, "restore-mongo", container_file]
            if db != "all":
                mongo_args.append(db)
            result = c.docker("mongodb", mongo_args)
        else:
            continue
        del result


def _evidence(c: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    actual = common.database_evidence(c)
    expected = manifest.get("database_evidence")
    if expected is None:
        raise RuntimeError("Backup has no deterministic database evidence")
    if actual != expected:
        raise RuntimeError("Restored database evidence does not match the backup manifest")
    return actual


def _write_report(c: Any, report: dict[str, Any]) -> None:
    state = Path(getattr(c, "state", getattr(c, "state_dir", Path(c.root) / ".backup-state")))
    state.mkdir(parents=True, exist_ok=True)
    path = state / "restore-report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _cleanup_sandbox(c: Any, env: dict[str, str], compose_file: Path) -> None:
    _compose(c, env, compose_file, ["down", "--volumes", "--remove-orphans"])
    volumes = c.run(["docker", "volume", "ls", "--quiet", "--filter", "label=com.docker.compose.project=" + env["COMPOSE_PROJECT_NAME"]]).decode().split()
    if volumes:
        c.run(["docker", "volume", "rm", *volumes])


def main(c: Any, argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    lock = c.lock() if hasattr(c, "lock") else contextlib.nullcontext()
    previous_handler = None

    def interrupt(signum, frame):
        raise InterruptedError("Restore interrupted by SIGTERM")

    try:
        previous_handler = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:  # Signal handlers can only be changed on the main thread.
        pass
    try:
        with lock:
            return _restore_locked(c, args)
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


def _restore_locked(c: Any, args: argparse.Namespace) -> int:
    started = time.monotonic()
    restore_id = uuid.uuid4().hex[:10]
    project = f"ogr-restore-{restore_id}"
    Path(c.state).mkdir(parents=True, exist_ok=True)
    state = Path(getattr(c, "state", getattr(c, "state_dir", Path(c.root) / ".backup-state")))
    restore_root = Path(tempfile.mkdtemp(prefix=f"{project}-", dir=str(state)))
    cleanup_succeeded = False
    report: dict[str, Any] = {"snapshot": args.snapshot, "project": project, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "checks": {}}
    try:
        snapshot = _resolve_snapshot(c, args.snapshot)
        report["snapshot"] = snapshot
        c.restic(["restore", snapshot, "--target", str(restore_root)])
        bundle = _find_bundle(restore_root)
        manifest = common.validate_bundle(bundle)
        report["checks"]["bundle"] = "passed"
        if args.verify_only:
            report["status"] = "verified"
            report["duration_seconds"] = round(time.monotonic() - started, 3)
            report["restore_directory"] = None
            _write_report(c, report)
            print(json.dumps(report, sort_keys=True))
            shutil.rmtree(restore_root, ignore_errors=True)
            return 0
        config_dir = bundle / "config"
        saved_env = _read_saved_env(config_dir)
        resolved_compose = _load_resolved_compose(bundle, manifest)
        _check_compatibility(manifest, saved_env, args, resolved_compose)
        report["checks"]["compatibility"] = "passed"
        isolated_env = _sandbox_env(saved_env, project, restore_root / ".backup-state", c.env)
        compose_file = _render_sandbox_compose(c, config_dir, bundle, isolated_env, restore_root, manifest)
        isolated_env["BACKUP_COMPOSE_FILES"] = compose_file.name
        sandbox_config = common.Config(root=restore_root, env=isolated_env)
        report["volume_evidence"] = _restore_volume_files(sandbox_config, bundle, manifest)
        report["checks"]["volume_archives"] = "passed"
        _restore_databases(sandbox_config, isolated_env, compose_file, bundle, manifest)
        report["checks"]["database_restore"] = "passed"
        report["database_evidence"] = _evidence(sandbox_config, manifest)
        report["checks"]["database_evidence"] = "passed"
        if args.start_apps:
            app_services = [name for name in json.loads(compose_file.read_text())["services"] if name != "ollama-init"]
            _compose(sandbox_config, isolated_env, compose_file, ["up", "-d", "--wait", "--wait-timeout", "300", *app_services])
            report["checks"]["application_start"] = "passed"
        if args.query_context:
            if not args.start_apps:
                raise RuntimeError("--query-context requires --start-apps")
            if "lightrag" not in json.loads(compose_file.read_text())["services"]:
                raise RuntimeError("--query-context requires a LightRAG service in the saved Compose configuration")
            query = json.dumps({"query": args.query_context, "mode": "mix", "enable_rerank": False})
            expected = args.expect_context
            check = "assert x.get('data') is not None"
            if expected is not None:
                check += "; assert " + repr(expected) + " in json.dumps(x,ensure_ascii=False)"
            code = "import json,urllib.request; r=urllib.request.Request('http://127.0.0.1:9621/query/data',data=" + repr(query.encode()) + ",headers={'Content-Type':'application/json','X-API-Key':__import__('os').environ['LIGHTRAG_API_KEY']},method='POST'); x=json.loads(urllib.request.urlopen(r,timeout=60).read()); " + check
            sandbox_config.docker("lightrag", ["python", "-c", code])
            report["checks"]["query_context"] = "passed"
        report["status"] = "complete"
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["rto_target_seconds"] = 7200
        report["rto_target_met"] = report["duration_seconds"] <= report["rto_target_seconds"]
        report["restore_directory"] = str(restore_root)
        _write_report(c, report)
        if args.cleanup:
            _cleanup_sandbox(sandbox_config, isolated_env, compose_file)
            cleanup_succeeded = True
            report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _write_report(c, report)
            last_drill = Path(c.state) / "last-drill.json"
            last_drill.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            last_drill.chmod(0o600)
        print(json.dumps(report, sort_keys=True))
        if args.cleanup and cleanup_succeeded:
            shutil.rmtree(restore_root, ignore_errors=True)
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        if args.verify_only:
            shutil.rmtree(restore_root, ignore_errors=True)
        elif args.cleanup:
            cleanup_config = locals().get("sandbox_config")
            cleanup_file = locals().get("compose_file")
            if cleanup_config is not None and cleanup_file is not None and not cleanup_succeeded:
                try:
                    _cleanup_sandbox(cleanup_config, isolated_env, cleanup_file)
                    cleanup_succeeded = True
                except Exception as cleanup_exc:
                    report["cleanup_error"] = str(cleanup_exc)
            if cleanup_succeeded:
                shutil.rmtree(restore_root, ignore_errors=True)
            else:
                report["restore_directory"] = str(restore_root)
        elif isinstance(exc, InterruptedError):
            cleanup_config = locals().get("sandbox_config")
            cleanup_file = locals().get("compose_file")
            if cleanup_config is not None and cleanup_file is not None:
                with contextlib.suppress(Exception):
                    _compose(cleanup_config, isolated_env, cleanup_file, ["down", "--remove-orphans"])
        try:
            _write_report(c, report)
        except Exception:
            pass
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(common.Config()))
    except RuntimeError as error:
        print(f"restore: {error}", file=sys.stderr)
        raise SystemExit(1)
