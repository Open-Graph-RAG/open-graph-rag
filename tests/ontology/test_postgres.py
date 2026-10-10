"""Opt-in integration tests; use a disposable database, never the stack database."""
import copy
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from services.ontology import migration
from services.ontology.store import Store, _connection
from services.ontology.validation import load_definition


@unittest.skipUnless(os.environ.get("ONTOLOGY_TEST_DATABASE_URL"), "Disposable PostgreSQL URL not supplied")
class PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.environ["ONTOLOGY_TEST_DATABASE_URL"])
        self.store.initialize()
        self.workspace = "test-" + uuid.uuid4().hex
        self.definition = load_definition({
            "id": "test", "version": "1.0.0", "status": "draft",
            "entities": {"System": {"description": "Software system", "properties": {"name": {"type": "string", "required": True}}}},
            "relations": {"DEPENDS_ON": {"source": ["System"], "target": ["System"], "directed": True,
                            "constraints": {"allow_self_reference": False, "source_max": 1}}},
        })
        self.store.save_draft(self.workspace, self.definition, "test")
        self.store.publish(self.workspace, "test", "1.0.0", "test")

    def fact(self, identifier="a", **extra):
        return {"id": identifier, "workspace": self.workspace, "kind": "entity", "ontology_id": "test",
                "ontology_version": "1.0.0", "entity_type": "System", "properties": {"name": identifier},
                "provenance": [{"document_id": "doc", "source_id": "src"}], **extra}

    def test_atomic_fact_outbox_and_immutable_version(self):
        first = self.store.write_fact(self.workspace, self.fact())
        self.assertEqual(first, self.store.write_fact(self.workspace, self.fact()))
        self.assertEqual([], self.store.list_facts("other"))
        self.assertEqual(1, len(self.store.list_sync(self.workspace)))
        with self.assertRaises(ValueError):
            self.store.save_draft(self.workspace, self.definition, "test")
        with self.assertRaises(ValueError):
            self.store.write_fact(self.workspace, self.fact("invalid", properties={}))
        self.store.write_fact(self.workspace, self.fact("invalid", properties={}), mode="quarantine")
        self.assertEqual(1, len(self.store.list_quarantine(self.workspace)))
        self.assertEqual(1, len(self.store.list_sync(self.workspace)))

    def test_cardinality_serializes_concurrent_writes(self):
        for identifier in ("a", "b", "c"):
            self.store.write_fact(self.workspace, self.fact(identifier))
        def write(identifier):
            try:
                self.store.write_fact(self.workspace, self.fact("r-" + identifier, kind="relation", properties={},
                    predicate="DEPENDS_ON", subject_id="a", object_id=identifier))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(1, sum(pool.map(write, ["b", "c"])))

    def test_migration_apply_and_rollback(self):
        self.store.write_fact(self.workspace, self.fact())
        target = copy.deepcopy(self.definition)
        target["version"] = "1.1.0"
        target["entities"]["System"]["properties"]["description"] = {"type": "string"}
        plan = migration.plan_migration(self.store, self.workspace, "test", "1.0.0", target, {}, "test")
        self.assertEqual("1.0.0", self.store.list_facts(self.workspace)[0]["ontology_version"])
        migration.apply_migration(self.store, self.workspace, plan["id"], "test", ontology_id="test")
        self.assertEqual("1.1.0", self.store.list_facts(self.workspace)[0]["ontology_version"])
        migration.rollback_migration(self.store, self.workspace, plan["id"], "test", ontology_id="test")
        self.assertEqual("1.0.0", self.store.list_facts(self.workspace)[0]["ontology_version"])

    def test_database_prevents_mutating_published_definitions(self):
        with self.assertRaises(Exception):
            with _connection(self.store.database_url) as conn, conn.cursor() as cur:
                cur.execute("UPDATE ontology.ontology_version SET definition='{}'::jsonb WHERE workspace=%s", (self.workspace,))
