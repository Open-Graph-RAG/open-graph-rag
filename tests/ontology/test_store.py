import unittest

from services.ontology.store import FactValidationError, MemoryStore, content_hash


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.definition = {
            "id": "test-ontology", "version": "1.0.0", "status": "draft",
            "entities": {"Person": {"description": "A person", "properties": {"name": {"type": "string", "required": True}}}},
            "relations": {},
        }
        self.store.save_draft("team-a", self.definition, "alice")
        self.store.publish("team-a", "test-ontology", "1.0.0", "alice")

    def fact(self, **extra):
        value = {
            "id": "person-1", "kind": "entity", "ontology_id": "test-ontology",
            "ontology_version": "1.0.0", "entity_type": "Person", "properties": {"name": "Ada"},
            "provenance": [{"document_id": "doc-1", "source_id": "source-1"}],
        }
        value.update(extra)
        return value

    def test_published_versions_are_immutable(self):
        changed = dict(self.definition, entities={})
        with self.assertRaises(ValueError):
            self.store.save_draft("team-a", changed, "alice")

    def test_fact_write_is_idempotent_and_workspace_scoped(self):
        fact = self.fact()
        first = self.store.write_fact("team-a", fact)
        second = self.store.write_fact("team-a", fact)
        self.assertEqual(first, second)
        self.assertEqual([first], self.store.list_facts("team-a"))
        self.assertEqual([], self.store.list_facts("team-b"))
        self.assertEqual(1, len(self.store.lease_outbox()))

    def test_invalid_fact_is_rejected_or_quarantined_without_projection(self):
        with self.assertRaises(ValueError):
            self.store.write_fact("team-a", self.fact(properties={}), mode="enforce")
        retained = self.store.write_fact("team-a", self.fact(id="person-2", properties={}), mode="quarantine")
        self.assertEqual("quarantined", retained["status"])
        self.assertEqual([], self.store.list_facts("team-a"))
        self.assertEqual([], self.store.lease_outbox())

    def test_reject_policy_overrides_quarantine_but_observe_keeps_candidate(self):
        definition = dict(self.definition, validation={"unknown_entity_type": "reject"})
        self.store = MemoryStore()
        self.store.save_draft("team-a", definition, "alice")
        self.store.publish("team-a", "test-ontology", "1.0.0", "alice")
        invalid = self.fact(id="person-3", entity_type="Unknown")
        with self.assertRaises(FactValidationError) as raised:
            self.store.write_fact("team-a", invalid, mode="quarantine")
        self.assertEqual("unknown_entity_type", raised.exception.errors[0]["code"])
        observed = self.store.write_fact("team-a", invalid, mode="observe")
        self.assertEqual("observed", observed["status"])
        self.assertEqual([], self.store.list_facts("team-a"))
        self.assertEqual(1, len(self.store.list_quarantine("team-a")))

    def test_publishing_another_version_does_not_switch_active(self):
        second = dict(self.definition, version="1.1.0")
        self.store.save_draft("team-a", second, "alice")
        self.store.publish("team-a", "test-ontology", "1.1.0", "alice")
        self.assertEqual("1.0.0", self.store.list_ontologies("team-a")[0]["active_version"])
        with self.assertRaises(ValueError):
            self.store.write_fact("team-a", self.fact(id="person-4", ontology_version="1.1.0"))

    def test_content_hash_ignores_workflow_status(self):
        self.assertEqual(content_hash(self.definition), content_hash(dict(self.definition, status="published")))


if __name__ == "__main__":
    unittest.main()
