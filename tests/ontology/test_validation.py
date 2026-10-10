import unittest
from pathlib import Path

from services.ontology.validation import (
    DefinitionValidationError,
    definition_hash,
    load_definition,
    validate_fact,
)


ROOT = Path(__file__).resolve().parents[2]


def definition():
    return {
        "id": "test-core",
        "version": "1.2.3",
        "status": "draft",
        "entities": {
            "Person": {
                "description": "A person",
                "properties": {
                    "name": {"type": "string", "required": True},
                    "age": {"type": "integer", "minimum": 0},
                    "status": {"type": "string", "enum": ["active", "inactive"]},
                },
            },
            "Team": {"description": "A team", "properties": {"name": {"type": "string", "required": True}}},
        },
        "relations": {
            "MEMBER_OF": {
                "source": ["Person"],
                "target": ["Team"],
                "directed": True,
                "constraints": {"source_max": 1, "allow_self_reference": False},
            }
        },
    }


def entity_fact(identifier, entity_type="Person", workspace="acme", properties=None):
    return {
        "id": identifier,
        "kind": "entity",
        "ontology_id": "test-core",
        "ontology_version": "1.2.3",
        "workspace": workspace,
        "entity_type": entity_type,
        "properties": properties or {"name": identifier},
        "provenance": [{"document_id": "doc-1", "source_id": "source-1"}],
    }


def relation_fact(identifier="edge-1", subject="person-1", object_id="team-1", workspace="acme"):
    return {
        "id": identifier,
        "kind": "relation",
        "ontology_id": "test-core",
        "ontology_version": "1.2.3",
        "workspace": workspace,
        "predicate": "MEMBER_OF",
        "subject_id": subject,
        "object_id": object_id,
        "properties": {},
        "provenance": [{"document_id": "doc-1", "source_id": "source-1", "confidence": 0.8}],
    }


class OntologyDefinitionTests(unittest.TestCase):
    def test_loads_ogr_core_yaml(self):
        loaded = load_definition((ROOT / "ontology/definitions/ogr-core/1.0.0.yaml").read_text())
        self.assertEqual(loaded["id"], "ogr-core")
        self.assertIn("SUPERSEDES", loaded["relations"])

    def test_rejects_unknown_relation_endpoint_and_bad_semver(self):
        candidate = definition()
        candidate["version"] = "01.2.3"
        candidate["relations"]["MEMBER_OF"]["target"] = ["Unknown"]
        with self.assertRaises(DefinitionValidationError) as raised:
            load_definition(candidate)
        codes = {error["code"] for error in raised.exception.errors}
        self.assertIn("schema_invalid", codes)
        self.assertIn("unknown_entity_type", codes)

    def test_rejects_malformed_prerelease_identifiers(self):
        for version in ("1.0.0-a..b", "1.0.0-a.", "1.0.0-01"):
            with self.subTest(version=version), self.assertRaises(DefinitionValidationError):
                load_definition({**definition(), "version": version})

    def test_rejects_property_keyword_and_enum_type_mismatches(self):
        candidate = definition()
        candidate["entities"]["Person"]["properties"]["age"]["minLength"] = 1
        candidate["entities"]["Person"]["properties"]["age"]["enum"] = ["old"]
        with self.assertRaises(DefinitionValidationError) as raised:
            load_definition(candidate)
        self.assertGreaterEqual(sum(error["code"] == "property_schema_invalid" for error in raised.exception.errors), 2)

    def test_malformed_property_types_and_bounds_return_validation_errors(self):
        candidate = definition()
        candidate["entities"]["Person"]["properties"]["age"] = {
            "type": ["string"], "minimum": [1], "maximum": [2], "enum": ["old"]
        }
        with self.assertRaises(DefinitionValidationError) as raised:
            load_definition(candidate)
        self.assertIn("schema_invalid", {error["code"] for error in raised.exception.errors})

    def test_rejects_non_json_yaml_values(self):
        for raw in (
            "id: test-core\nversion: 1.2.3\nstatus: draft\nentities: {}\nrelations: {}\nextra: .nan\n",
            "id: test-core\nversion: 1.2.3\nstatus: draft\nentities:\n  Person:\n    description: A person\n    properties:\n      birth_date:\n        type: string\n        enum: [2026-10-09]\nrelations: {}\n",
        ):
            with self.subTest(raw=raw), self.assertRaises(DefinitionValidationError) as raised:
                load_definition(raw)
            self.assertEqual(raised.exception.errors[0]["code"], "invalid_json")

    def test_hash_ignores_status_but_includes_content(self):
        draft = definition()
        published = {**draft, "status": "published"}
        self.assertEqual(definition_hash(draft), definition_hash(published))
        self.assertEqual(definition_hash(draft), definition_hash({**published, "content_hash": "stored-digest"}))
        self.assertEqual(definition_hash(draft), definition_hash({**published, "hash": "stored-digest"}))
        changed = {**published, "version": "1.2.4"}
        self.assertNotEqual(definition_hash(draft), definition_hash(changed))


class FactValidationTests(unittest.TestCase):
    def setUp(self):
        self.definition = definition()
        self.entities = {
            "person-1": entity_fact("person-1"),
            "team-1": entity_fact("team-1", "Team"),
        }

    def codes(self, errors):
        return {error["code"] for error in errors}

    def test_accepts_valid_entity_and_relation(self):
        self.assertEqual(validate_fact(self.definition, entity_fact("person-2"), self.entities, []), [])
        self.assertEqual(validate_fact(self.definition, relation_fact(), self.entities, []), [])

    def test_required_property_type_and_enum_constraints(self):
        fact = entity_fact("person-2", properties={"age": "old", "status": "unknown", "extra": True})
        errors = validate_fact(self.definition, fact, self.entities, [])
        self.assertIn("invalid_property", self.codes(errors))
        self.assertTrue(any(error["code"] == "missing_required_property" and error["field"] == "properties.name" for error in errors))

    def test_nested_required_property_markers_compile_to_json_schema(self):
        self.definition["entities"]["Person"]["properties"]["profile"] = {
            "type": "object",
            "properties": {"department": {"type": "string", "required": True}},
        }
        self.definition = load_definition(self.definition)
        errors = validate_fact(self.definition, entity_fact("person-2", properties={"name": "Ada", "profile": {}}), self.entities, [])
        self.assertTrue(any(error["field"] == "properties.profile.department" for error in errors))

    def test_relation_checks_endpoint_type_workspace_and_self_reference(self):
        errors = validate_fact(self.definition, relation_fact(subject="team-1"), self.entities, [])
        self.assertIn("endpoint_type_mismatch", self.codes(errors))
        errors = validate_fact(self.definition, relation_fact(object_id="missing"), self.entities, [])
        self.assertIn("missing_endpoint", self.codes(errors))
        self.entities["person-1"]["workspace"] = "other"
        errors = validate_fact(self.definition, relation_fact(), self.entities, [])
        self.assertIn("workspace_mismatch", self.codes(errors))
        self.entities["person-1"].update({"workspace": "acme", "ontology_id": "other-core"})
        errors = validate_fact(self.definition, relation_fact(), self.entities, [])
        self.assertIn("ontology_mismatch", self.codes(errors))
        self.entities["person-1"].update({"ontology_id": "test-core", "ontology_version": "1.0.0"})
        errors = validate_fact(self.definition, relation_fact(), self.entities, [])
        self.assertIn("ontology_version_mismatch", self.codes(errors))

    def test_duplicate_id_cardinality_and_provenance_limits(self):
        existing = [relation_fact(identifier="edge-old")]
        errors = validate_fact(self.definition, relation_fact(), self.entities, existing)
        self.assertIn("cardinality_exceeded", self.codes(errors))
        errors = validate_fact(self.definition, entity_fact("person-1"), self.entities, [])
        self.assertIn("duplicate_fact_id", self.codes(errors))
        invalid = relation_fact()
        invalid["provenance"] = [{"document_id": "", "source_id": "src", "confidence": 1.1}]
        errors = validate_fact(self.definition, invalid, self.entities, [])
        self.assertIn("invalid_provenance", self.codes(errors))
        self.assertIn("invalid_confidence", self.codes(errors))

    def test_malformed_facts_return_errors(self):
        errors = validate_fact(self.definition, {"kind": [], "properties": [], "provenance": [None]}, self.entities, [])
        self.assertTrue(errors)
        self.assertTrue(all(set(error) == {"code", "field", "message"} for error in errors))

    def test_non_finite_fact_property_returns_stable_error(self):
        fact = entity_fact("person-2", properties={"name": "Ada", "score": float("nan")})
        errors = validate_fact(self.definition, fact, self.entities, [])
        self.assertEqual(errors[0]["code"], "invalid_json")


if __name__ == "__main__":
    unittest.main()
