"""Docker/Restic recovery test using disposable synthetic application data."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock


BACKUP_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "backup"
sys.path.insert(0, str(BACKUP_SCRIPTS))
import backup_engine  # noqa: E402
import common  # noqa: E402
import restore_engine  # noqa: E402


PG_IMAGE = "pgvector/pgvector:0.8.0-pg17@sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87"
AP_PG_IMAGE = "pgvector/pgvector:0.8.0-pg14@sha256:c55d7e7deac05dde62139e0ded4fcf4f58363656cbc382dbea82fbed995aa767"
MONGO_IMAGE = "mongo:8.0.20@sha256:098862b1339f031900ca66cf8fef799e616d6324fa41b9a263f2ec899552c1ef"


def available(command: list[str]) -> bool:
    try:
        return subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        ).returncode == 0
    except FileNotFoundError:
        return False


@unittest.skipUnless(os.environ.get("BACKUP_E2E") == "1", "set BACKUP_E2E=1 to run Docker/Restic recovery")
class RestoreEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        if not available(["docker", "compose", "version"]):
            self.skipTest("Docker Compose is unavailable")
        if not available(["restic", "version"]):
            self.skipTest("Restic is unavailable")

        self.temp = tempfile.TemporaryDirectory(prefix="ogr-backup-e2e-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "private-state"
        self.primary = self.root / "restic-primary"
        self.offsite = self.root / "restic-offsite"
        self.password_file = self.root / "restic-password"
        self.password_file.write_text("synthetic-e2e-password\n", encoding="utf-8")
        self.password_file.chmod(0o600)
        (self.root / ".env").write_text(
            "POSTGRES_USER=fixture\nPOSTGRES_PASSWORD=fixture-postgres-password\n"
            "POSTGRES_DB=fixture\nMONGO_USER=root\nMONGO_PASSWORD=fixture-mongo-password\n"
            "MONGO_INITDB_ROOT_USERNAME=root\nMONGO_INITDB_ROOT_PASSWORD=fixture-mongo-password\n"
            "LIGHTRAG_WORKSPACE=e2e-fixture\nEMBEDDING_MODEL=fixture-model\nEMBEDDING_DIM=3\n"
            "AP_POSTGRES_DATABASE=apfixture\nAP_POSTGRES_USERNAME=apfixture\n"
            "AP_POSTGRES_PASSWORD=fixture-ap-password\nAP_ENCRYPTION_KEY=fixture-encryption-key\n"
            "AP_JWT_SECRET=fixture-jwt-secret\n",
            encoding="utf-8",
        )
        (self.root / "compose.yaml").write_text(
            f"""services:
  postgres:
    image: {PG_IMAGE}
    environment:
      POSTGRES_USER: ${{POSTGRES_USER}}
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD}}
      POSTGRES_DB: ${{POSTGRES_DB}}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: [CMD-SHELL, 'pg_isready -U "$${{POSTGRES_USER}}"']
      interval: 2s
      timeout: 2s
      retries: 30
  mongodb:
    image: {MONGO_IMAGE}
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${{MONGO_INITDB_ROOT_USERNAME}}
      MONGO_INITDB_ROOT_PASSWORD: ${{MONGO_INITDB_ROOT_PASSWORD}}
    volumes:
      - mongo_data:/data/db
    healthcheck:
      test: [CMD-SHELL, 'mongosh --quiet --username "$${{MONGO_INITDB_ROOT_USERNAME}}" --password "$${{MONGO_INITDB_ROOT_PASSWORD}}" --authenticationDatabase admin --eval "db.adminCommand({{ping:1}}).ok"']
      interval: 2s
      timeout: 3s
      retries: 40
  writer:
    image: {PG_IMAGE}
    command: [sh, -c, 'printf "synthetic-original-document\\n" > /data/original-document.txt; printf "enc:v1:synthetic-secret-marker\\n" > /data/encrypted-secret.marker; sleep infinity']
    volumes:
      - fixture_data:/data
volumes:
  postgres_data: {{}}
  mongo_data: {{}}
  fixture_data: {{}}
""",
            encoding="utf-8",
        )
        (self.root / "compose.activepieces.yaml").write_text(
            f"""services:
  activepieces-postgres:
    image: {AP_PG_IMAGE}
    environment:
      POSTGRES_USER: ${{AP_POSTGRES_USERNAME}}
      POSTGRES_PASSWORD: ${{AP_POSTGRES_PASSWORD}}
      POSTGRES_DB: ${{AP_POSTGRES_DATABASE}}
    volumes:
      - activepieces_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: [CMD-SHELL, 'pg_isready -U "$${{POSTGRES_USER}}" -d "$${{POSTGRES_DB}}"']
      interval: 2s
      timeout: 2s
      retries: 30
  activepieces-redis:
    image: redis:7.0.7@sha256:bb474c35022ca2c5618f4c49ca759bd2c0eea1daf5d934c560bd30092b97b498
    command: [redis-server, --appendonly, "yes"]
    volumes:
      - activepieces_redis_data:/data
    healthcheck:
      test: [CMD, redis-cli, ping]
      interval: 2s
      timeout: 2s
      retries: 30
volumes:
  activepieces_postgres_data: {{}}
  activepieces_redis_data: {{}}
""",
            encoding="utf-8",
        )
        config = self.root / "backup.env"
        config.write_text(
            "\n".join(
                [
                    f"BACKUP_STATE_DIR={self.state}",
                    "BACKUP_COMPOSE_FILES=compose.yaml:compose.activepieces.yaml",
                    f"RESTIC_REPOSITORY={self.primary}",
                    f"RESTIC_PASSWORD_FILE={self.password_file}",
                    f"BACKUP_OFFSITE_REPOSITORY={self.offsite}",
                    "BACKUP_MIN_FREE_BYTES=0",
                    "POSTGRES_USER=fixture",
                    "POSTGRES_PASSWORD=fixture-postgres-password",
                    "POSTGRES_DB=fixture",
                    "MONGO_USER=root",
                    "MONGO_PASSWORD=fixture-mongo-password",
                    "MONGO_INITDB_ROOT_USERNAME=root",
                    "MONGO_INITDB_ROOT_PASSWORD=fixture-mongo-password",
                    "LIGHTRAG_WORKSPACE=e2e-fixture",
                    "EMBEDDING_MODEL=fixture-model",
                    "EMBEDDING_DIM=3",
                    "AP_POSTGRES_DATABASE=apfixture",
                    "AP_POSTGRES_USERNAME=apfixture",
                    "AP_POSTGRES_PASSWORD=fixture-ap-password",
                    "AP_ENCRYPTION_KEY=fixture-encryption-key",
                    "AP_JWT_SECRET=fixture-jwt-secret",
                    f"COMPOSE_PROJECT_NAME=ogr-backup-e2e-{uuid.uuid4().hex[:10]}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        runtime_env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
            if key in os.environ
        }
        runtime_env["BACKUP_CONFIG"] = str(config)
        self.config = common.Config(root=self.root, env=runtime_env)
        self.project = self.config.env["COMPOSE_PROJECT_NAME"]
        self.addCleanup(self._cleanup_project)
        self.config.restic(["init"])
        self.config.restic(["init", "--repo", str(self.offsite)])
        self.config.run(self.config.compose + ["up", "-d", "--wait", "--wait-timeout", "180"])
        self._seed_fixture()

    def _cleanup_project(self) -> None:
        if hasattr(self, "config"):
            with contextlib.suppress(Exception):
                self.config.run(self.config.compose + ["down", "--volumes", "--remove-orphans"])

    def _seed_fixture(self) -> None:
        sql = """CREATE EXTENSION vector;
CREATE TABLE graph_rows(id text PRIMARY KEY, payload jsonb);
CREATE TABLE graph_edges(source_id text, target_id text, relation text);
CREATE TABLE chunk_rows(id text PRIMARY KEY, content text);
CREATE TABLE vector_rows(id text PRIMARY KEY, embedding vector(3));
CREATE TABLE status_rows(id text PRIMARY KEY, status text);
INSERT INTO graph_rows VALUES ('node-001', '{"name":"Acme Fixture","kind":"company"}');
INSERT INTO graph_rows VALUES ('node-002', '{"name":"Synthetic Contact","kind":"person"}');
INSERT INTO graph_edges VALUES ('node-001', 'node-002', 'contact');
INSERT INTO chunk_rows VALUES ('chunk-001', 'synthetic source evidence');
INSERT INTO vector_rows VALUES ('vector-001', '[0.1,0.2,0.3]'::vector);
INSERT INTO status_rows VALUES ('doc-001', 'processed');"""
        self.config.docker(
            "postgres",
            ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "fixture", "-d", "fixture", "-c", sql],
        )
        ap_sql = """CREATE TABLE workflow_rows(id text PRIMARY KEY, name text, encrypted_secret text);
INSERT INTO workflow_rows VALUES ('flow-001', 'Synthetic workflow', 'encrypted-fixture-secret-v1');"""
        self.config.docker(
            "activepieces-postgres",
            ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "apfixture", "-d", "apfixture", "-c", ap_sql],
        )
        self.config.docker("activepieces-redis", ["redis-cli", "set", "fixture:marker", "redis-fixture-v1"])
        mongo_script = (
            'const d=db.getSiblingDB("LibreChat");'
            'd.users.insertOne({_id:"fixture-user",name:"Synthetic User"});'
            'd.conversations.insertOne({_id:"fixture-conversation",userId:"fixture-user",text:"fixture chat"});'
            'd.workflows.insertOne({_id:"fixture-workflow",name:"fixture workflow"});'
        )
        self.config.docker(
            "mongodb",
            [
                "sh",
                "-c",
                'exec mongosh --quiet --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --eval "$1"',
                "sh",
                mongo_script,
            ],
        )
        marker = self.config.docker("writer", ["sh", "-c", "cat /data/original-document.txt"])
        self.assertEqual(marker.decode().strip(), "synthetic-original-document")
        encrypted = self.config.docker("writer", ["sh", "-c", "cat /data/encrypted-secret.marker"])
        self.assertEqual(encrypted.decode().strip(), "enc:v1:synthetic-secret-marker")

        self.expected_retrieval = self._postgres_query(
            "SELECT id FROM vector_rows ORDER BY embedding <-> '[0,0,0]'::vector LIMIT 1"
        )
        self.expected_chunk = self._postgres_query("SELECT content FROM chunk_rows WHERE id='chunk-001'")
        self.expected_relation = self._postgres_query(
            "SELECT source_id || '->' || target_id || ':' || relation FROM graph_edges"
        )
        self.assertEqual(self.expected_retrieval, "vector-001")
        self.assertEqual(self.expected_chunk, "synthetic source evidence")
        self.assertEqual(self.expected_relation, "node-001->node-002:contact")

    def _postgres_query(self, sql: str) -> str:
        return self._query_postgres(self.config, sql)

    def _query_postgres(self, config: common.Config, sql: str) -> str:
        return config.docker(
            "postgres", ["psql", "-X", "-A", "-t", "-U", "fixture", "-d", "fixture", "-c", sql]
        ).decode().strip()

    def test_backup_restores_synthetic_stack_and_rejects_corrupt_bundle(self) -> None:
        e2e_started = time.monotonic()
        self._check_bad_repository_password()
        self._check_missing_offsite_fails_before_quiescing()
        backup_out = io.StringIO()
        with contextlib.redirect_stdout(backup_out):
            self.assertEqual(backup_engine.main(self.config, ["backup"]), 0)
        snapshots = json.loads(self.config.restic(["snapshots", "--json", "--tag", "ogr-backup"]))
        self.assertTrue(snapshots)
        snapshot_id = snapshots[-1]["id"]

        original_restore_databases = restore_engine._restore_databases
        restored_file_contents: list[str] = []

        def check_restored_file(*args, **kwargs):
            original_restore_databases(*args, **kwargs)
            sandbox_config = args[0]
            env = args[1]
            compose_file = args[2]
            volume_name = f"{env['COMPOSE_PROJECT_NAME']}_fixture_data"
            result = self.config.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    f"{volume_name}:/fixture:ro",
                    "--entrypoint",
                    "cat",
                    PG_IMAGE,
                    "/fixture/original-document.txt",
                ]
            )
            restored_file_contents.append(result.decode().strip())
            encrypted = self.config.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    f"{volume_name}:/fixture:ro",
                    "--entrypoint",
                    "cat",
                    PG_IMAGE,
                    "/fixture/encrypted-secret.marker",
                ]
            )
            restored_file_contents.append(encrypted.decode().strip())
            restored_compose = [
                "docker",
                "compose",
                "--project-name",
                env["COMPOSE_PROJECT_NAME"],
                "-f",
                str(compose_file),
            ]
            workflow = self.config.run(
                restored_compose
                + [
                    "exec",
                    "-T",
                    "activepieces-postgres",
                    "psql",
                    "-X",
                    "-A",
                    "-t",
                    "-U",
                    "apfixture",
                    "-d",
                    "apfixture",
                    "-c",
                    "SELECT encrypted_secret FROM workflow_rows WHERE id='flow-001'",
                ]
            )
            restored_file_contents.append(workflow.decode().strip())
            redis_marker = self.config.run(
                restored_compose
                + ["exec", "-T", "activepieces-redis", "redis-cli", "get", "fixture:marker"]
            )
            restored_file_contents.append(redis_marker.decode().strip())
            restored_file_contents.extend(
                [
                    self._query_postgres(
                        sandbox_config,
                        "SELECT id FROM vector_rows ORDER BY embedding <-> '[0,0,0]'::vector LIMIT 1",
                    ),
                    self._query_postgres(sandbox_config, "SELECT content FROM chunk_rows WHERE id='chunk-001'"),
                    self._query_postgres(
                        sandbox_config,
                        "SELECT source_id || '->' || target_id || ':' || relation FROM graph_edges",
                    ),
                ]
            )

        restore_out = io.StringIO()
        with mock.patch.object(restore_engine, "_restore_databases", side_effect=check_restored_file):
            with contextlib.redirect_stdout(restore_out):
                self.assertEqual(
                    restore_engine.main(
                        self.config,
                        ["--snapshot", snapshot_id, "--target", "isolated", "--cleanup"],
                    ),
                    0,
                )
        report = json.loads(restore_out.getvalue())
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["checks"]["database_evidence"], "passed")
        self.assertEqual(report["database_evidence"], self._backup_evidence(snapshot_id))
        self.assertEqual(
            restored_file_contents,
            [
                "synthetic-original-document",
                "enc:v1:synthetic-secret-marker",
                "encrypted-fixture-secret-v1",
                "redis-fixture-v1",
                self.expected_retrieval,
                self.expected_chunk,
                self.expected_relation,
            ],
        )

        # The cleanup flag must remove every volume owned by the isolated project.
        self.assertEqual(
            self.config.run(
                ["docker", "volume", "ls", "--quiet", "--filter", f"name={report['project']}_"]
            ).decode().split(),
            [],
        )

        corrupted_root = self.root / "corrupted-input"
        corrupted_root.mkdir()
        self.config.restic(["restore", snapshot_id, "--target", str(corrupted_root)])
        bundle = next(corrupted_root.rglob("manifest.json")).parent
        dump = next((bundle / "dumps").rglob("*.dump"))
        content = bytearray(dump.read_bytes())
        content[len(content) // 2] ^= 0x01
        dump.write_bytes(content)
        corrupted_snapshot = backup_engine._extract_snapshot_id(
            self.config.restic(["backup", "--tag", "ogr-backup", "--json", str(bundle)])
        )
        verify_out = io.StringIO()
        verify_err = io.StringIO()
        with contextlib.redirect_stdout(verify_out), contextlib.redirect_stderr(verify_err):
            self.assertEqual(
                restore_engine.main(self.config, ["--snapshot", corrupted_snapshot, "--verify-only"]),
                1,
            )
        self.assertIn("checksum mismatch", verify_err.getvalue().lower())
        print(
            "Synthetic isolated restore: "
            f"{report['duration_seconds']:.3f}s; target met: {report['rto_target_met']}; "
            f"backup/restore/corruption E2E total: {time.monotonic() - e2e_started:.3f}s"
        )

    def _check_bad_repository_password(self) -> None:
        bad_password = self.root / "wrong-restic-password"
        bad_password.write_text("incorrect synthetic password\n", encoding="utf-8")
        bad_password.chmod(0o600)
        env = {key: value for key, value in self.config.env.items() if key != "BACKUP_CONFIG"}
        env["RESTIC_PASSWORD_FILE"] = str(bad_password)
        bad_config = common.Config(root=self.root, env=env)
        with self.assertRaisesRegex(RuntimeError, "Backup command failed: restic"):
            bad_config.restic(["snapshots", "--json"])

    def _check_missing_offsite_fails_before_quiescing(self) -> None:
        env = {key: value for key, value in self.config.env.items() if key != "BACKUP_CONFIG"}
        env["BACKUP_STATE_DIR"] = str(self.root / "missing-offsite-state")
        env["BACKUP_OFFSITE_REPOSITORY"] = str(self.root / "missing-offsite-repository")
        config = common.Config(root=self.root, env=env)
        running_before = set(self.config.running())
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(backup_engine.main(config, ["backup"]), 1)
        self.assertIn("restic", output.getvalue().lower())
        self.assertEqual(set(self.config.running()), running_before)

    def _backup_evidence(self, snapshot_id: str) -> dict:
        extracted = self.root / "evidence"
        extracted.mkdir()
        self.config.restic(["restore", snapshot_id, "--target", str(extracted)])
        manifest = json.loads(next(extracted.rglob("manifest.json")).read_text(encoding="utf-8"))
        return manifest["database_evidence"]


if __name__ == "__main__":
    unittest.main()
