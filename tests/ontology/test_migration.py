import os
import unittest
import uuid
from unittest.mock import patch

from services.ontology import validation
from services.ontology.migration import _renamed_fact
from services.ontology.migration import apply_migration, plan_migration, rollback_migration
from services.ontology.store import MemoryStore, Store, _connection
from services.ontology.validation import load_definition


class MigrationTransformTests(unittest.TestCase):
    def populated_store(self):
        store = MemoryStore()
        old = {
            "id": "test-ontology", "version": "1.0.0", "status": "draft",
            "entities": {"Person": {"description": "A person", "properties": {"name": {"type": "string"}}}},
            "relations": {},
        }
        store.save_draft("team-a", old, "alice")
        store.publish("team-a", old["id"], old["version"], "alice")
        fact = {"id": "person-1", "kind": "entity", "ontology_id": old["id"], "ontology_version": old["version"],
                "workspace": "team-a", "entity_type": "Person", "properties": {"name": "Ada"},
                "provenance": [{"document_id": "doc-1", "source_id": "source-1"}]}
        store.write_fact("team-a", fact)
        return store, old

    def test_renames_only_explicit_semantic_names_and_versions(self):
        entity = {"id": "e1", "kind": "entity", "entity_type": "Person", "ontology_version": "1.0.0"}
        relation = {"id": "r1", "kind": "relation", "predicate": "OWNS", "ontology_version": "1.0.0"}
        mapping = {"entities": {"Person": "Human"}, "relations": {"OWNS": "OWNS_ASSET"}}
        self.assertEqual({**entity, "entity_type": "Human", "ontology_version": "2.0.0"}, _renamed_fact(entity, mapping, "2.0.0"))
        self.assertEqual({**relation, "predicate": "OWNS_ASSET", "ontology_version": "2.0.0"}, _renamed_fact(relation, mapping, "2.0.0"))
        unchanged = _renamed_fact(entity, {}, "2.0.0")
        self.assertEqual("Person", unchanged["entity_type"])
        self.assertEqual("1.0.0", entity["ontology_version"])

    def test_dry_run_apply_and_rollback_preserve_snapshot(self):
        store, old = self.populated_store()
        new = {**old, "version": "2.0.0", "entities": {"Human": old["entities"]["Person"]}}
        plan = plan_migration(store, "team-a", old["id"], old["version"], new, {"entities": {"Person": "Human"}}, "alice")
        self.assertTrue(plan["valid"])
        self.assertTrue(plan["diff"]["breaking"])
        self.assertEqual("Person", store.list_facts("team-a")[0]["entity_type"])
        lease = store.lease_outbox()[0]
        apply_migration(store, "team-a", plan["id"], "alice", ontology_id=old["id"])
        with self.assertRaises(KeyError):
            store.complete_outbox(lease["id"], lease["lease_token"])
        with store.projection_guard(lease) as current:
            self.assertFalse(current)
        changed = store.list_facts("team-a")[0]
        self.assertEqual(("Human", "2.0.0"), (changed["entity_type"], changed["ontology_version"]))
        self.assertEqual("2.0.0", store.list_ontologies("team-a")[0]["active_version"])
        with self.assertRaises(ValueError):
            store.write_fact("team-a", {**changed, "id": "person-old", "ontology_version": "1.0.0"})
        result = rollback_migration(store, "team-a", plan["id"], "alice", ontology_id=old["id"])
        self.assertEqual("rolled_back", result["state"])
        restored = store.list_facts("team-a")[0]
        self.assertEqual(("Person", "1.0.0"), (restored["entity_type"], restored["ontology_version"]))
        self.assertEqual("1.0.0", store.list_ontologies("team-a")[0]["active_version"])

    def test_optional_property_change_is_compatible(self):
        store, old = self.populated_store()
        new = {**old, "version": "1.1.0", "entities": {"Person": {**old["entities"]["Person"],
            "properties": {**old["entities"]["Person"]["properties"], "nickname": {"type": "string"}}}}}
        plan = plan_migration(store, "team-a", old["id"], old["version"], new, {}, "alice")
        self.assertTrue(plan["valid"])
        self.assertFalse(plan["diff"]["breaking"])
        self.assertIn({"category": "property", "entity": "Person", "name": "nickname", "change": "added", "breaking": False}, plan["diff"]["changes"])

    def test_invalid_target_returns_blocked_reviewable_plan(self):
        store, old = self.populated_store()
        new = {**old, "version": "2.0.0", "entities": {"Person": {**old["entities"]["Person"],
            "properties": {**old["entities"]["Person"]["properties"], "email": {"type": "string", "required": True}}}}}
        plan = plan_migration(store, "team-a", old["id"], old["version"], new, {}, "alice")
        self.assertFalse(plan["valid"])
        self.assertEqual("blocked", plan["state"])
        self.assertTrue(plan["errors"])
        with self.assertRaises(ValueError):
            apply_migration(store, "team-a", plan["id"], "alice")
        repaired = plan_migration(store, "team-a", old["id"], old["version"], new,
                                  {"fact_properties": {"person-1": {"name": "Ada", "email": "ada@example.test"}}}, "alice")
        self.assertTrue(repaired["valid"])
        self.assertTrue(repaired["diff"]["breaking"])
        apply_migration(store, "team-a", repaired["id"], "alice")
        migrated = store.list_facts("team-a")[0]
        self.assertEqual({"name": "Ada", "email": "ada@example.test"}, migrated["properties"])
        rollback_migration(store, "team-a", repaired["id"], "alice")

    def test_stale_dry_run_is_refused(self):
        store, old = self.populated_store()
        new = {**old, "version": "1.1.0"}
        plan = plan_migration(store, "team-a", old["id"], old["version"], new, {}, "alice")
        added = {"id": "person-2", "kind": "entity", "ontology_id": old["id"], "ontology_version": old["version"],
                 "workspace": "team-a", "entity_type": "Person", "properties": {"name": "Grace"},
                 "provenance": [{"document_id": "doc-2", "source_id": "source-2"}]}
        store.write_fact("team-a", added)
        with self.assertRaisesRegex(ValueError, "changed after dry-run"):
            apply_migration(store, "team-a", plan["id"], "alice")

    def test_rollback_refuses_newer_writes(self):
        store, old = self.populated_store()
        new = {**old, "version": "1.1.0"}
        plan = plan_migration(store, "team-a", old["id"], old["version"], new, {}, "alice")
        apply_migration(store, "team-a", plan["id"], "alice")
        added = {"id": "person-2", "kind": "entity", "ontology_id": old["id"], "ontology_version": "1.1.0",
                 "workspace": "team-a", "entity_type": "Person", "properties": {"name": "Grace"},
                 "provenance": [{"document_id": "doc-2", "source_id": "source-2"}]}
        store.write_fact("team-a", added)
        with self.assertRaisesRegex(ValueError, "rollback would overwrite newer writes"):
            rollback_migration(store, "team-a", plan["id"], "alice")

    def test_existing_identical_published_target_is_reused(self):
        store, old = self.populated_store()
        new = {**old, "version": "1.1.0"}
        store.save_draft("team-a", new, "alice")
        store.publish("team-a", old["id"], new["version"], "alice")
        plan = plan_migration(store, "team-a", old["id"], old["version"], new, {}, "alice")
        apply_migration(store, "team-a", plan["id"], "alice")
        self.assertEqual("1.1.0", store.list_facts("team-a")[0]["ontology_version"])


@unittest.skipUnless(os.environ.get("ONTOLOGY_TEST_DATABASE_URL"), "Disposable PostgreSQL URL not supplied")
class PostgresPlanSnapshotTests(unittest.TestCase):
    def test_same_count_record_change_during_validation_invalidates_plan(self):
        store = Store(os.environ["ONTOLOGY_TEST_DATABASE_URL"])
        store.initialize()
        workspace = "test-" + uuid.uuid4().hex
        definition = load_definition({
            "id": "snapshot-test", "version": "1.0.0", "status": "draft",
            "entities": {"Person": {"description": "A person", "properties": {"name": {"type": "string"}}}},
            "relations": {},
        })
        store.save_draft(workspace, definition, "test")
        store.publish(workspace, definition["id"], definition["version"], "test")
        fact = {"id": "person-1", "kind": "entity", "ontology_id": definition["id"],
                "ontology_version": definition["version"], "workspace": workspace, "entity_type": "Person",
                "properties": {"name": "Ada"},
                "provenance": [{"document_id": "doc-1", "source_id": "source-1"}]}
        store.write_fact(workspace, fact)
        target = {**definition, "version": "1.1.0"}
        original_validate = validation.validate_fact
        changed = False

        def validate_then_mutate(*args, **kwargs):
            nonlocal changed
            result = original_validate(*args, **kwargs)
            if not changed:
                changed = True
                with _connection(store.database_url) as conn, conn.cursor() as cur:
                    cur.execute("UPDATE ontology.fact SET record=jsonb_set(record,'{properties,name}','\"Grace\"'::jsonb) WHERE workspace=%s AND id='person-1'", (workspace,))
            return result

        with patch.object(validation, "validate_fact", side_effect=validate_then_mutate):
            with self.assertRaisesRegex(ValueError, "facts changed while migration was being planned"):
                plan_migration(store, workspace, definition["id"], definition["version"], target, {}, "test")


if __name__ == "__main__":
    unittest.main()
