"""Transactional PostgreSQL registry and canonical fact store."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Mapping


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Mapping[str, Any]) -> str:
    stable = dict(value)
    stable.pop("status", None)
    return hashlib.sha256(_canonical(stable).encode("utf-8")).hexdigest()


class FactValidationError(ValueError):
    """A canonical fact failed deterministic ontology validation."""

    def __init__(self, errors: list[Any]):
        self.errors = errors
        super().__init__("Canonical fact validation failed")


def _connection(url: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised in deployment images
        raise RuntimeError("Install psycopg[binary] to use the PostgreSQL ontology store") from exc
    return psycopg.connect(url, row_factory=dict_row)


class Store:
    """PostgreSQL-backed workspace-isolated ontology and fact store."""

    def __init__(self, database_url: str):
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url

    def initialize(self) -> None:
        """Apply the idempotent registry schema migration."""
        from pathlib import Path
        migration = Path(__file__).resolve().parents[2] / "postgres" / "migrations" / "001_ontology.sql"
        with _connection(self.database_url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('ontology-schema-migrations', 0))")
            conn.execute(migration.read_text(encoding="utf-8"))

    @staticmethod
    def _lock(cur, workspace: str, ontology_id: str = "") -> None:
        # Serialize all canonical graph changes in a workspace. Fact IDs and relation
        # endpoint/cardinality constraints are workspace-wide, so per-ontology locks
        # would allow races between different ontology IDs.
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (workspace,))

    @staticmethod
    def _audit(cur, workspace: str, actor: str, action: str, subject: str | None, details: Any = None) -> None:
        from psycopg.types.json import Jsonb
        cur.execute(
            "INSERT INTO ontology.audit(workspace, actor, action, subject, details) VALUES (%s,%s,%s,%s,%s)",
            (workspace, actor, action, subject, Jsonb(details or {})),
        )

    def list_ontologies(self, workspace: str) -> list[dict[str, Any]]:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("""SELECT o.id, o.active_version, o.created_at, o.updated_at,
                v.version AS latest_version, v.status AS latest_status
                FROM ontology.ontology o LEFT JOIN LATERAL (
                    SELECT version,status FROM ontology.ontology_version
                    WHERE workspace=o.workspace AND ontology_id=o.id
                    ORDER BY created_at DESC LIMIT 1
                ) v ON true WHERE o.workspace=%s ORDER BY o.id""", (workspace,))
            return [dict(r) for r in cur.fetchall()]

    def list_versions(self, workspace: str, ontology_id: str) -> list[dict[str, Any]]:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("""SELECT version,status,content_hash,created_by,created_at,published_by,published_at
                FROM ontology.ontology_version WHERE workspace=%s AND ontology_id=%s
                ORDER BY created_at DESC""", (workspace, ontology_id))
            return [dict(r) for r in cur.fetchall()]

    def get_version(self, workspace: str, ontology_id: str, version: str) -> dict[str, Any] | None:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT definition,status FROM ontology.ontology_version WHERE workspace=%s AND ontology_id=%s AND version=%s",
                        (workspace, ontology_id, version))
            row = cur.fetchone()
            return {**dict(row["definition"]), "status": row["status"]} if row else None

    def save_draft(self, workspace: str, definition: Mapping[str, Any], actor: str) -> dict[str, Any]:
        from psycopg.types.json import Jsonb
        data = deepcopy(dict(definition))
        ontology_id, version = data.get("id"), data.get("version")
        if not isinstance(ontology_id, str) or not ontology_id or not isinstance(version, str) or not version:
            raise ValueError("definition must include non-empty id and version")
        data["status"] = "draft"
        from services.ontology.validation import load_definition
        data = load_definition(data)
        digest = content_hash(data)
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            self._lock(cur, workspace, ontology_id)
            cur.execute("INSERT INTO ontology.ontology(workspace,id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (workspace, ontology_id))
            cur.execute("SELECT status FROM ontology.ontology_version WHERE workspace=%s AND ontology_id=%s AND version=%s FOR UPDATE",
                        (workspace, ontology_id, version))
            old = cur.fetchone()
            if old and old["status"] == "published":
                raise ValueError("published ontology versions are immutable")
            if old:
                cur.execute("UPDATE ontology.ontology_version SET definition=%s,content_hash=%s,created_by=%s,created_at=now() WHERE workspace=%s AND ontology_id=%s AND version=%s",
                            (Jsonb(data), digest, actor, workspace, ontology_id, version))
            else:
                cur.execute("INSERT INTO ontology.ontology_version(workspace,ontology_id,version,status,definition,content_hash,created_by) VALUES (%s,%s,%s,'draft',%s,%s,%s)",
                            (workspace, ontology_id, version, Jsonb(data), digest, actor))
            cur.execute("UPDATE ontology.ontology SET updated_at=now() WHERE workspace=%s AND id=%s", (workspace, ontology_id))
            self._audit(cur, workspace, actor, "ontology.draft_saved", f"{ontology_id}@{version}", {"content_hash": digest})
        return data

    def publish(self, workspace: str, ontology_id: str, version: str, actor: str) -> dict[str, Any]:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            self._lock(cur, workspace, ontology_id)
            cur.execute("SELECT status,definition,content_hash FROM ontology.ontology_version WHERE workspace=%s AND ontology_id=%s AND version=%s FOR UPDATE",
                        (workspace, ontology_id, version))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"unknown ontology version {ontology_id}@{version}")
            if row["status"] == "published":
                return {**dict(row["definition"]), "status": "published"}
            cur.execute("UPDATE ontology.ontology_version SET status='published',published_by=%s,published_at=now() WHERE workspace=%s AND ontology_id=%s AND version=%s",
                        (actor, workspace, ontology_id, version))
            cur.execute("UPDATE ontology.ontology SET active_version=%s,updated_at=now() WHERE workspace=%s AND id=%s AND active_version IS NULL",
                        (version, workspace, ontology_id))
            self._audit(cur, workspace, actor, "ontology.published", f"{ontology_id}@{version}", {"content_hash": row["content_hash"]})
            return {**dict(row["definition"]), "status": "published"}

    def list_facts(self, workspace: str, ontology_id: str | None = None) -> list[dict[str, Any]]:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            if ontology_id:
                cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s AND ontology_id=%s ORDER BY created_at,id", (workspace, ontology_id))
            else:
                cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s ORDER BY created_at,id", (workspace,))
            return [dict(r["record"]) for r in cur.fetchall()]

    def write_fact(self, workspace: str, fact: Mapping[str, Any], errors=None, mode="enforce", actor="system") -> dict[str, Any]:
        from psycopg.types.json import Jsonb
        if mode not in {"enforce", "quarantine", "observe"}:
            raise ValueError("mode must be enforce, quarantine, or observe")
        record = deepcopy(dict(fact))
        record.pop("status", None)
        record.setdefault("id", str(uuid.uuid4()))
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("workspace must be a nonempty string")
        if not isinstance(record["id"], str) or not _IDENTIFIER.fullmatch(record["id"]):
            raise FactValidationError([{"code": "invalid_identifier", "field": "id", "message": "Fact id must be a nonempty stable identifier."}])
        record["workspace"] = workspace
        ontology_id = record.get("ontology_id")
        if not isinstance(ontology_id, str) or not ontology_id:
            raise FactValidationError([{"code": "invalid_ontology_id", "field": "ontology_id", "message": "Fact must include a nonempty ontology_id."}])
        if record.get("ontology_version") is not None and not isinstance(record["ontology_version"], str):
            raise FactValidationError([{"code": "invalid_ontology_version", "field": "ontology_version", "message": "ontology_version must be a string."}])
        record.pop("directed", None)
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            self._lock(cur, workspace, ontology_id)
            version = record.get("ontology_version")
            if not version:
                cur.execute("SELECT active_version FROM ontology.ontology WHERE workspace=%s AND id=%s", (workspace, ontology_id))
                current = cur.fetchone()
                if not current or not current["active_version"]:
                    raise ValueError("ontology has no active published version")
                version = record["ontology_version"] = current["active_version"]
            cur.execute("SELECT active_version FROM ontology.ontology WHERE workspace=%s AND id=%s", (workspace, ontology_id))
            active = cur.fetchone()
            if not active or active["active_version"] != version:
                raise ValueError("fact ontology_version is not the active ontology version")
            cur.execute("SELECT definition FROM ontology.ontology_version WHERE workspace=%s AND ontology_id=%s AND version=%s AND status='published'",
                        (workspace, ontology_id, version))
            ontology = cur.fetchone()
            if not ontology:
                raise ValueError("facts must reference a published ontology version")
            if record.get("kind") == "relation":
                predicate = record.get("predicate")
                relation_definition = dict(ontology["definition"]).get("relations", {}).get(predicate) if isinstance(predicate, str) else None
                if relation_definition:
                    record["directed"] = relation_definition["directed"]
            cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s AND id=%s", (workspace, record["id"]))
            prior = cur.fetchone()
            if prior:
                candidate = {**record, "status": "accepted"}
                if dict(prior["record"]) != candidate:
                    raise ValueError("fact id already exists with different content")
                return dict(prior["record"])
            cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s", (workspace,))
            existing = [dict(r["record"]) for r in cur.fetchall()]
            ontology_facts = [f for f in existing if f.get("ontology_id") == ontology_id]
            entities = {f["id"]: f for f in ontology_facts if f.get("kind") == "entity"}
            # Import lazily to avoid a package cycle and validate against the exact locked snapshot.
            from services.ontology.validation import validate_fact
            validation_errors = list(validate_fact(dict(ontology["definition"]), record, entities, ontology_facts) or [])
            if errors:
                validation_errors.extend(e for e in errors if e not in validation_errors)
            if validation_errors:
                if mode == "enforce":
                    raise FactValidationError(validation_errors)
                if mode == "quarantine":
                    policies = dict(ontology["definition"]).get("validation", {})
                    rejected = [error for error in validation_errors if isinstance(error, dict) and policies.get(error.get("code")) == "reject"]
                    if rejected:
                        raise FactValidationError(rejected)
                cur.execute("INSERT INTO ontology.quarantine(workspace,id,ontology_id,record,errors,mode,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (workspace, record["id"], ontology_id, Jsonb(record), Jsonb(validation_errors), mode, actor))
                self._audit(cur, workspace, actor, f"fact.{mode}", record["id"], {"errors": validation_errors})
                return {**record, "status": "observed" if mode == "observe" else "quarantined", "errors": validation_errors}
            record["status"] = "accepted"
            cur.execute("""INSERT INTO ontology.fact(workspace,id,ontology_id,ontology_version,kind,record,created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace,id) DO NOTHING RETURNING id""",
                (workspace, record["id"], ontology_id, version, record.get("kind"), Jsonb(record), actor))
            inserted = cur.fetchone()
            if inserted:
                cur.execute("INSERT INTO ontology.outbox(workspace,fact_id,payload) VALUES (%s,%s,%s) ON CONFLICT(workspace,fact_id) DO NOTHING",
                            (workspace, record["id"], Jsonb(record)))
                self._audit(cur, workspace, actor, "fact.accepted", record["id"], {"ontology": ontology_id, "version": version})
            else:
                cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s AND id=%s", (workspace, record["id"]))
                prior = cur.fetchone()
                if prior["record"] != record:
                    raise ValueError("fact id already exists with different content")
                record = dict(prior["record"])
        return record

    def audit(self, workspace: str) -> list[dict[str, Any]]:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT sequence,actor,action,subject,details,created_at FROM ontology.audit WHERE workspace=%s ORDER BY sequence", (workspace,))
            return [dict(r) for r in cur.fetchall()]

    def list_quarantine(self, workspace: str) -> list[dict[str, Any]]:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT id,ontology_id,record,errors,mode,created_by,created_at FROM ontology.quarantine WHERE workspace=%s ORDER BY created_at,id", (workspace,))
            return [dict(r) for r in cur.fetchall()]

    def list_sync(self, workspace: str) -> list[dict[str, Any]]:
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT id,fact_id,attempts,available_at,leased_until,delivered_at,last_error FROM ontology.outbox WHERE workspace=%s ORDER BY id", (workspace,))
            return [dict(r) for r in cur.fetchall()]

    def lease_outbox(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        token = str(uuid.uuid4())
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT workspace FROM ontology.outbox WHERE delivered_at IS NULL AND available_at<=now() AND (leased_until IS NULL OR leased_until<now()) ORDER BY id LIMIT 1")
            candidate = cur.fetchone()
            if not candidate:
                return []
            candidate_workspace = candidate["workspace"]
            self._lock(cur, candidate_workspace)
            cur.execute("""WITH picked AS (SELECT id FROM ontology.outbox WHERE delivered_at IS NULL
                AND workspace=%s AND available_at<=now() AND (leased_until IS NULL OR leased_until<now())
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE ontology.outbox o SET leased_until=now()+interval '60 seconds',lease_token=%s,attempts=attempts+1
                FROM picked WHERE o.id=picked.id RETURNING o.id,o.workspace,o.payload""", (candidate_workspace, limit, token))
            return [{"id": r["id"], "workspace": r["workspace"], "fact": dict(r["payload"]), "lease_token": token} for r in cur.fetchall()]

    @contextmanager
    def projection_guard(self, item: Mapping[str, Any]):
        """Hold the workspace lock while projecting the exact currently leased payload."""
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            self._lock(cur, str(item["workspace"]))
            cur.execute("SELECT payload,lease_token,delivered_at FROM ontology.outbox WHERE id=%s AND workspace=%s",
                        (item["id"], item["workspace"]))
            row = cur.fetchone()
            valid = bool(row and not row["delivered_at"] and row["lease_token"] == item.get("lease_token")
                         and dict(row["payload"]) == dict(item["fact"]))
            yield valid

    def complete_outbox(self, outbox_id: int, lease_token: str | None = None) -> None:
        if not lease_token:
            raise ValueError("lease_token is required to complete a leased outbox item")
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("UPDATE ontology.outbox SET delivered_at=now(),leased_until=NULL,lease_token=NULL WHERE id=%s AND delivered_at IS NULL AND lease_token=%s", (outbox_id, lease_token))
            if cur.rowcount != 1:
                raise KeyError(f"outbox item {outbox_id} is missing or no longer leased")

    def fail_outbox(self, outbox_id: int, error: str, lease_token: str | None = None) -> None:
        if not lease_token:
            raise ValueError("lease_token is required to fail a leased outbox item")
        with _connection(self.database_url) as conn, conn.cursor() as cur:
            cur.execute("""UPDATE ontology.outbox SET available_at=now()+make_interval(secs=>LEAST(3600,power(2,LEAST(attempts,12))::integer)),
                leased_until=NULL,lease_token=NULL,last_error=%s WHERE id=%s AND delivered_at IS NULL AND lease_token=%s""",
                (str(error)[:2000], outbox_id, lease_token))
            if cur.rowcount != 1:
                raise KeyError(f"outbox item {outbox_id} is missing or no longer leased")


class MemoryStore:
    """Small in-memory implementation for unit tests and API contract tests only."""

    def __init__(self, database_url: str = "memory://"):
        self._versions: dict[tuple[str, str, str], dict] = {}
        self._active_versions: dict[tuple[str, str], str] = {}
        self._facts: dict[tuple[str, str], dict] = {}
        self._quarantine: list[dict] = []
        self._audits: list[dict] = []
        self._outbox: list[dict] = []
        self._plans: dict[tuple[str, str], dict] = {}
        self._snapshots: dict[tuple[str, str], list[dict]] = {}
        self._applied_snapshots: dict[tuple[str, str], list[dict]] = {}

    def initialize(self):
        """No-op for parity with the production Store lifecycle."""
        return None

    def list_ontologies(self, workspace):
        ids = {i for w, i, _ in self._versions if w == workspace}
        return [{"id": i, "active_version": self._active_versions.get((workspace, i))} for i in sorted(ids)]

    def list_versions(self, workspace, ontology_id):
        return [{"version": v["version"], "status": v["status"], "content_hash": v["content_hash"]}
                for (w, i, _), v in self._versions.items() if w == workspace and i == ontology_id]

    def get_version(self, workspace, ontology_id, version):
        row = self._versions.get((workspace, ontology_id, version))
        return {**deepcopy(row["definition"]), "status": row["status"]} if row else None

    def save_draft(self, workspace, definition, actor):
        data = deepcopy(dict(definition)); data["status"] = "draft"
        key = (workspace, data["id"], data["version"])
        old = self._versions.get(key)
        if old and old["status"] == "published": raise ValueError("published ontology versions are immutable")
        row = {"definition": data, "version": data["version"], "status": "draft", "content_hash": content_hash(data)}
        self._versions[key] = row
        self._audits.append({"workspace": workspace, "actor": actor, "action": "ontology.draft_saved", "subject": f"{data['id']}@{data['version']}"})
        return deepcopy(data)

    def publish(self, workspace, ontology_id, version, actor):
        row = self._versions.get((workspace, ontology_id, version))
        if not row: raise KeyError(f"unknown ontology version {ontology_id}@{version}")
        row["status"] = "published"
        self._active_versions.setdefault((workspace, ontology_id), version)
        return {**deepcopy(row["definition"]), "status": "published"}

    def list_facts(self, workspace, ontology_id=None):
        return [deepcopy(f) for (w, _), f in self._facts.items() if w == workspace and (ontology_id is None or f["ontology_id"] == ontology_id)]

    def write_fact(self, workspace, fact, errors=None, mode="enforce", actor="system"):
        data = deepcopy(dict(fact)); data.pop("status", None); data.setdefault("id", str(uuid.uuid4()))
        if not isinstance(workspace, str) or not workspace.strip(): raise ValueError("workspace must be a nonempty string")
        if not isinstance(data["id"], str) or not _IDENTIFIER.fullmatch(data["id"]):
            raise FactValidationError([{"code": "invalid_identifier", "field": "id", "message": "Fact id must be a nonempty stable identifier."}])
        data["workspace"] = workspace
        data.pop("directed", None)
        if not isinstance(data.get("ontology_id"), str) or not data["ontology_id"]:
            raise FactValidationError([{"code": "invalid_ontology_id", "field": "ontology_id", "message": "Fact must include a nonempty ontology_id."}])
        if data.get("ontology_version") is not None and not isinstance(data["ontology_version"], str):
            raise FactValidationError([{"code": "invalid_ontology_version", "field": "ontology_version", "message": "ontology_version must be a string."}])
        version = data.get("ontology_version")
        if not version:
            version = self._active_versions.get((workspace, data["ontology_id"]))
            if not version: raise ValueError("ontology has no active published version")
            version = data["ontology_version"] = version
        if self._active_versions.get((workspace, data["ontology_id"])) != version:
            raise ValueError("fact ontology_version is not the active ontology version")
        definition = self.get_version(workspace, data["ontology_id"], version)
        if not definition or self._versions[(workspace, data["ontology_id"], version)]["status"] != "published": raise ValueError("facts must reference a published ontology version")
        if data.get("kind") == "relation":
            predicate = data.get("predicate")
            relation_definition = definition.get("relations", {}).get(predicate) if isinstance(predicate, str) else None
            if relation_definition: data["directed"] = relation_definition["directed"]
        prior = self._facts.get((workspace, data["id"]))
        if prior:
            candidate = {**data, "status": "accepted"}
            if prior != candidate: raise ValueError("fact id already exists with different content")
            return deepcopy(prior)
        from services.ontology.validation import validate_fact
        existing = self.list_facts(workspace)
        ontology_facts = [f for f in existing if f.get("ontology_id") == data["ontology_id"]]
        entities = {f["id"]: f for f in ontology_facts if f.get("kind") == "entity"}
        found = list(validate_fact(definition, data, entities, ontology_facts) or [])
        if errors: found.extend(e for e in errors if e not in found)
        if found:
            if mode == "enforce": raise FactValidationError(found)
            if mode == "quarantine":
                policies = definition.get("validation", {})
                rejected = [error for error in found if isinstance(error, dict) and policies.get(error.get("code")) == "reject"]
                if rejected: raise FactValidationError(rejected)
            self._quarantine.append({"workspace": workspace, "fact": data, "errors": found, "mode": mode})
            self._audits.append({"workspace": workspace, "actor": actor, "action": f"fact.{mode}", "subject": data["id"]})
            return {**data, "status": "observed" if mode == "observe" else "quarantined", "errors": found}
        data["status"] = "accepted"
        key = (workspace, data["id"])
        prior = self._facts.get(key)
        if prior and prior != data: raise ValueError("fact id already exists with different content")
        if not prior:
            self._facts[key] = deepcopy(data); self._outbox.append({"id": len(self._outbox)+1, "workspace": workspace, "fact": deepcopy(data), "attempts": 0})
            self._audits.append({"workspace": workspace, "actor": actor, "action": "fact.accepted", "subject": data["id"]})
        return deepcopy(self._facts[key])

    def audit(self, workspace): return [deepcopy(x) for x in self._audits if x.get("workspace") == workspace]

    def list_quarantine(self, workspace): return [deepcopy(x) for x in self._quarantine if x.get("workspace") == workspace]

    def list_sync(self, workspace):
        return [{"id": x["id"], "fact_id": x["fact"]["id"], "attempts": x["attempts"],
                 "delivered_at": True if x.get("delivered") else None, "last_error": x.get("error")}
                for x in self._outbox if x.get("workspace") == workspace]

    def lease_outbox(self, limit=10):
        selected = [x for x in self._outbox if not x.get("delivered") and not x.get("leased")][:limit]
        leased = []
        for x in selected:
            x["leased"] = True; x["attempts"] += 1; x["lease_token"] = str(uuid.uuid4())
            leased.append({"id": x["id"], "workspace": x["workspace"], "fact": deepcopy(x["fact"]), "lease_token": x["lease_token"]})
        return leased

    @contextmanager
    def projection_guard(self, item):
        row = next((x for x in self._outbox if x["id"] == item["id"] and x["workspace"] == item["workspace"]), None)
        valid = bool(row and not row.get("delivered") and row.get("lease_token") == item.get("lease_token")
                     and row.get("fact") == item.get("fact"))
        yield valid

    def complete_outbox(self, outbox_id, lease_token=None):
        row = next((x for x in self._outbox if x["id"] == outbox_id), None)
        if not row or row.get("delivered") or not lease_token or row.get("lease_token") != lease_token: raise KeyError(outbox_id)
        row["delivered"] = True; row["leased"] = False; row["lease_token"] = None

    def fail_outbox(self, outbox_id, error, lease_token=None):
        row = next((x for x in self._outbox if x["id"] == outbox_id), None)
        if not row or row.get("delivered") or not lease_token or row.get("lease_token") != lease_token: raise KeyError(outbox_id)
        row["leased"] = False; row["lease_token"] = None; row["error"] = str(error)
