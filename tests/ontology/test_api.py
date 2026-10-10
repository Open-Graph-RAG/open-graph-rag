import unittest

from fastapi.testclient import TestClient

from services.ontology.api import create_app
from services.ontology.store import MemoryStore


ADMIN_TOKEN = "a" * 40
READER_TOKEN = "r" * 40
OTHER_TOKEN = "o" * 40


def ontology(version="1.0.0"):
    return {
        "id": "api-core",
        "version": version,
        "status": "draft",
        "entities": {
            "Person": {
                "description": "A person",
                "properties": {"name": {"type": "string", "required": True}},
            },
            "Team": {
                "description": "A team",
                "properties": {"name": {"type": "string", "required": True}},
            },
        },
        "relations": {
            "MEMBER_OF": {
                "source": ["Person"],
                "target": ["Team"],
                "directed": True,
                "constraints": {"source_max": 1},
            }
        },
    }


def fact(identifier, kind="entity", entity_type="Person", **fields):
    value = {
        "id": identifier,
        "kind": kind,
        "ontology_id": "api-core",
        "ontology_version": "1.0.0",
        "workspace": "alpha",
        "properties": {"name": identifier} if kind == "entity" else {},
        "provenance": [{"document_id": "doc-1", "source_id": "source-1", "confidence": 0.9}],
    }
    if kind == "entity":
        value["entity_type"] = entity_type
    value.update(fields)
    return value


class OntologyApiTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.app = create_app(
            self.store,
            {
                ADMIN_TOKEN: {"role": "ontology_admin", "actor": "admin-user", "workspaces": ["alpha"]},
                READER_TOKEN: {"role": "ontology_reader", "actor": "reader-user", "workspaces": ["alpha"]},
                OTHER_TOKEN: {"role": "ontology_admin", "workspaces": ["beta"]},
            },
            "alpha",
        )
        # No lifespan is needed for the in-memory store; production initialization is PostgreSQL-only.
        self.client = TestClient(self.app)

    def headers(self, token=ADMIN_TOKEN, workspace="alpha"):
        return {"Authorization": f"Bearer {token}", "X-Workspace": workspace}

    def register_and_publish(self):
        response = self.client.post("/v1/ontologies", headers=self.headers(), json={"definition": ontology()})
        self.assertEqual(response.status_code, 201, response.text)
        response = self.client.post("/v1/ontologies/api-core/publish", headers=self.headers(), json={"version": "1.0.0"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_authentication_role_and_workspace_guards(self):
        self.assertEqual(self.client.get("/v1/ontologies").status_code, 401)
        self.assertEqual(self.client.get("/v1/ontologies", headers=self.headers(workspace="beta")).status_code, 403)
        self.assertEqual(self.client.get("/v1/ontologies", headers=self.headers(OTHER_TOKEN, "alpha")).status_code, 403)
        self.assertEqual(
            self.client.post("/v1/ontologies", headers=self.headers(READER_TOKEN), json={"definition": ontology()}).status_code,
            403,
        )
        self.register_and_publish()
        self.assertEqual(
            self.client.post("/v1/ontologies/api-core/publish", headers=self.headers(READER_TOKEN), json={"version": "1.0.0"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post("/v1/facts", headers=self.headers(READER_TOKEN), json={"fact": fact("person-1")}).status_code,
            403,
        )

    def test_registry_validation_and_published_versions_are_readable(self):
        definition = ontology()
        validation = self.client.post(
            "/v1/ontologies/api-core/validate", headers=self.headers(READER_TOKEN), json={"definition": definition}
        )
        self.assertEqual(validation.status_code, 200, validation.text)
        self.assertTrue(validation.json()["valid"])
        self.register_and_publish()
        versions = self.client.get("/v1/ontologies/api-core/versions", headers=self.headers(READER_TOKEN))
        self.assertEqual(versions.status_code, 200)
        self.assertEqual(versions.json()[0]["status"], "published")
        retrieved = self.client.get("/v1/ontologies/api-core/versions/1.0.0", headers=self.headers(READER_TOKEN))
        self.assertEqual(retrieved.status_code, 200)
        self.assertEqual(retrieved.json()["status"], "published")

    def test_valid_facts_quarantine_and_projection_status(self):
        self.register_and_publish()
        alice = fact("person-1")
        team = fact("team-1", entity_type="Team")
        relation = fact(
            "membership-1",
            kind="relation",
            predicate="MEMBER_OF",
            subject_id="person-1",
            object_id="team-1",
        )
        for candidate in (alice, team, relation):
            validation = self.client.post("/v1/facts/validate", headers=self.headers(READER_TOKEN), json={"fact": candidate})
            self.assertEqual(validation.status_code, 200, validation.text)
            self.assertTrue(validation.json()["valid"], validation.json())
            written = self.client.post("/v1/facts", headers=self.headers(), json={"fact": candidate})
            self.assertEqual(written.status_code, 201, written.text)
            self.assertEqual(written.json()["status"], "accepted")

        invalid = fact("person-invalid", properties={})
        quarantined = self.client.post(
            "/v1/facts", headers=self.headers(), json={"fact": invalid, "mode": "quarantine"}
        )
        self.assertEqual(quarantined.status_code, 201, quarantined.text)
        self.assertEqual(quarantined.json()["status"], "quarantined")
        self.assertTrue(any(error["code"] == "missing_required_property" for error in quarantined.json()["errors"]))

        accepted = self.client.get("/v1/facts", headers=self.headers(READER_TOKEN))
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual({item["id"] for item in accepted.json()}, {"person-1", "team-1", "membership-1"})
        quarantine = self.client.get("/v1/quarantine", headers=self.headers(READER_TOKEN))
        self.assertEqual(quarantine.status_code, 200, quarantine.text)
        self.assertEqual(len(quarantine.json()), 1)
        projection = self.client.get("/v1/projection-status", headers=self.headers(READER_TOKEN))
        self.assertEqual(projection.status_code, 200, projection.text)
        self.assertEqual(len(projection.json()), 3)

    def test_fact_workspace_mismatch_is_denied(self):
        self.register_and_publish()
        candidate = fact("foreign-person", workspace="beta")
        response = self.client.post("/v1/facts/validate", headers=self.headers(READER_TOKEN), json={"fact": candidate})
        self.assertEqual(response.status_code, 403)

    def test_malformed_fact_shapes_are_rejected_with_422(self):
        headers = self.headers(READER_TOKEN)
        response = self.client.post("/v1/facts/validate", headers=headers, json={"fact": []})
        self.assertEqual(response.status_code, 422)
        malformed_id = fact("person-1")
        malformed_id["id"] = []
        response = self.client.post("/v1/facts/validate", headers=headers, json={"fact": malformed_id})
        self.assertEqual(response.status_code, 422)

    def test_missing_and_unknown_ontology_versions_return_404(self):
        headers = self.headers(READER_TOKEN)
        self.assertEqual(self.client.get("/v1/ontologies/api-core/versions/9.9.9", headers=headers).status_code, 404)
        candidate = fact("person-1")
        candidate["ontology_version"] = "9.9.9"
        response = self.client.post("/v1/facts/validate", headers=headers, json={"fact": candidate})
        self.assertEqual(response.status_code, 404)

    def test_oversized_mutation_body_returns_413(self):
        response = self.client.post(
            "/v1/ontologies",
            headers={**self.headers(), "Content-Type": "application/json"},
            content=b" " * (1_048_577),
        )
        self.assertEqual(response.status_code, 413)

    def test_migration_plan_apply_and_rollback_are_admin_only(self):
        self.register_and_publish()
        created = self.client.post("/v1/facts", headers=self.headers(), json={"fact": fact("person-1")})
        self.assertEqual(created.status_code, 201, created.text)
        target = ontology("2.0.0")
        target["entities"]["Human"] = target["entities"].pop("Person")
        target["relations"]["MEMBER_OF"]["source"] = ["Human"]
        planned = self.client.post(
            "/v1/ontologies/api-core/migrations/plan",
            headers=self.headers(),
            json={"from_version": "1.0.0", "definition": target, "renames": {"entities": {"Person": "Human"}}},
        )
        self.assertEqual(planned.status_code, 200, planned.text)
        plan = planned.json()
        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["facts"], 1)
        self.assertEqual(
            self.client.post(
                "/v1/ontologies/api-core/migrations/apply",
                headers=self.headers(READER_TOKEN),
                json={"plan_id": plan["id"]},
            ).status_code,
            403,
        )
        applied = self.client.post(
            "/v1/ontologies/api-core/migrations/apply", headers=self.headers(), json={"plan_id": plan["id"]}
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["state"], "applied")
        migrated = self.client.get("/v1/facts", headers=self.headers(READER_TOKEN)).json()[0]
        self.assertEqual(migrated["entity_type"], "Human")
        self.assertEqual(migrated["ontology_version"], "2.0.0")
        rolled_back = self.client.post(
            "/v1/ontologies/api-core/migrations/rollback", headers=self.headers(), json={"migration_id": plan["id"]}
        )
        self.assertEqual(rolled_back.status_code, 200, rolled_back.text)
        restored = self.client.get("/v1/facts", headers=self.headers(READER_TOKEN)).json()[0]
        self.assertEqual(restored["entity_type"], "Person")
        self.assertEqual(restored["ontology_version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
