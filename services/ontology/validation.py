"""Deterministic ontology and canonical-fact validation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import jsonschema
import yaml


_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "ontology" / "schemas" / "ontology.schema.json"
_ONTOLOGY_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")


class DefinitionValidationError(ValueError):
    """Raised when an ontology document is malformed or internally inconsistent."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(error["message"] for error in errors))


def _path(parts: Any) -> str:
    return ".".join(str(part) for part in parts) or "$"


def _relation_types(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else value


def _property_json_schema(properties: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
    required = []
    for name, definition in properties.items():
        schema["properties"][name] = _compile_property(definition)
        if isinstance(definition, dict) and definition.get("required") is True:
            required.append(name)
    if required:
        schema["required"] = required
    return schema, required


def _compile_property(definition: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(definition, dict):
        return {}
    result = {key: value for key, value in definition.items() if key not in {"required", "description"}}
    nested_properties = result.get("properties")
    if isinstance(nested_properties, dict):
        compiled = {}
        required = []
        for name, nested in nested_properties.items():
            compiled[name] = _compile_property(nested)
            if isinstance(nested, dict) and nested.get("required") is True:
                required.append(name)
        result["properties"] = compiled
        if required:
            result["required"] = required
    if isinstance(result.get("items"), dict):
        result["items"] = _compile_property(result["items"])
    return result


def _property_authoring_errors(properties: dict[str, Any], prefix: str) -> list[dict[str, str]]:
    errors = []
    typed_keywords = {
        "string": {"minLength", "maxLength"},
        "integer": {"minimum", "maximum"},
        "number": {"minimum", "maximum"},
        "object": {"properties", "additionalProperties"},
        "array": {"items"},
    }
    for name, definition in properties.items():
        field = f"{prefix}.{name}"
        if not isinstance(definition, dict):
            continue  # The ontology JSON Schema reports the structural error.
        value_type = definition.get("type")
        permitted = typed_keywords.get(value_type, set())
        for keyword in set(definition) & {"minLength", "maxLength", "minimum", "maximum", "properties", "additionalProperties", "items"}:
            if keyword not in permitted:
                errors.append({"code": "property_schema_invalid", "field": f"{field}.{keyword}", "message": f"{keyword} is not valid for property type {value_type!r}."})
        for lower, upper in (("minLength", "maxLength"), ("minimum", "maximum")):
            if lower in definition and upper in definition and definition[lower] > definition[upper]:
                errors.append({"code": "property_schema_invalid", "field": field, "message": f"{lower} must not exceed {upper}."})
        if "enum" in definition:
            checker = jsonschema.Draft202012Validator({"type": value_type})
            if any(not checker.is_valid(value) for value in definition["enum"]):
                errors.append({"code": "property_schema_invalid", "field": f"{field}.enum", "message": f"Enum values must match property type {value_type!r}."})
        nested = definition.get("properties")
        if isinstance(nested, dict):
            errors.extend(_property_authoring_errors(nested, f"{field}.properties"))
        item = definition.get("items")
        if isinstance(item, dict):
            errors.extend(_property_authoring_errors({"items": item}, field))
    return errors


def load_definition(raw: dict | str) -> dict:
    """Parse and validate an ontology mapping or YAML/JSON string.

    Property ``required: true`` authoring markers are translated to JSON Schema's
    object-level ``required`` list when facts are checked.
    """
    errors: list[dict[str, str]] = []
    if isinstance(raw, str):
        try:
            definition = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise DefinitionValidationError([{"code": "invalid_yaml", "field": "$", "message": str(exc)}]) from exc
    elif isinstance(raw, dict):
        definition = copy.deepcopy(raw)
    else:
        raise DefinitionValidationError([{"code": "invalid_definition", "field": "$", "message": "Definition must be a mapping or YAML string."}])

    if not isinstance(definition, dict):
        raise DefinitionValidationError([{"code": "invalid_definition", "field": "$", "message": "Definition must contain a mapping at its root."}])

    validator = jsonschema.Draft202012Validator(_ONTOLOGY_SCHEMA)
    for error in sorted(validator.iter_errors(definition), key=lambda item: (_path(item.absolute_path), item.validator or "")):
        errors.append({"code": "schema_invalid", "field": _path(error.absolute_path), "message": error.message})

    # Check duplicate SemVer build-independent prerelease numeric identifiers.
    version = definition.get("version")
    if isinstance(version, str):
        core = version.split("+", 1)[0]
        prerelease = core.split("-", 1)[1] if "-" in core else ""
        if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")):
            errors.append({"code": "invalid_semver", "field": "version", "message": "Numeric prerelease identifiers must not contain leading zeroes."})

    entities = definition.get("entities")
    relations = definition.get("relations")
    if isinstance(entities, dict) and isinstance(relations, dict):
        for relation_name, relation in relations.items():
            if not isinstance(relation, dict):
                continue
            for endpoint in ("source", "target"):
                allowed = relation.get(endpoint)
                if isinstance(allowed, (str, list)):
                    unknown = [name for name in _relation_types(allowed) if name not in entities]
                    for name in unknown:
                        errors.append({
                            "code": "unknown_entity_type",
                            "field": f"relations.{relation_name}.{endpoint}",
                            "message": f"Relation {relation_name} references unknown entity type {name!r}.",
                        })
        for entity_name, entity in entities.items():
            if not isinstance(entity, dict) or not isinstance(entity.get("properties"), dict):
                continue
            properties = entity["properties"]
            errors.extend(_property_authoring_errors(properties, f"entities.{entity_name}.properties"))
            try:
                property_schema, _ = _property_json_schema(properties)
                jsonschema.Draft202012Validator.check_schema(property_schema)
            except (jsonschema.SchemaError, AttributeError, TypeError) as exc:
                errors.append({
                    "code": "property_schema_invalid",
                    "field": f"entities.{entity_name}.properties",
                    "message": str(exc),
                })
    if errors:
        raise DefinitionValidationError(errors)
    return definition


def definition_hash(definition: dict) -> str:
    """Return a stable SHA-256 content hash, independent of draft/published status."""
    canonical = copy.deepcopy(definition)
    for metadata_key in ("status", "hash", "content_hash"):
        canonical.pop(metadata_key, None)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _error(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _entity_info(value: Any) -> tuple[Any, Any, Any, Any]:
    if not isinstance(value, dict):
        return None, None, None, None
    return value.get("entity_type"), value.get("workspace"), value.get("ontology_id"), value.get("ontology_version")


def validate_fact(
    definition: dict,
    fact: dict,
    entities: dict[str, dict],
    facts: list[dict],
    *,
    check_uniqueness: bool = True,
) -> list[dict]:
    """Return stable, machine-readable errors for a canonical entity/relation fact."""
    errors: list[dict[str, str]] = []
    if not isinstance(definition, dict):
        return [_error("invalid_definition", "$", "Ontology definition must be a mapping.")]
    if not isinstance(fact, dict):
        return [_error("invalid_fact", "$", "Fact must be a mapping.")]
    if not isinstance(entities, dict):
        entities = {}
    if not isinstance(facts, list):
        facts = []

    fact_id = fact.get("id")
    if not _valid_identifier(fact_id):
        errors.append(_error("invalid_identifier", "id", "Fact id must be a nonempty stable identifier (1-255 permitted characters)."))
    else:
        if check_uniqueness and fact_id in entities:
            errors.append(_error("duplicate_fact_id", "id", f"Fact id {fact_id!r} already exists."))
        if check_uniqueness:
            for existing in facts:
                if isinstance(existing, dict) and existing.get("id") == fact_id:
                    errors.append(_error("duplicate_fact_id", "id", f"Fact id {fact_id!r} already exists."))
                    break

    kind = fact.get("kind")
    if kind not in ("entity", "relation"):
        errors.append(_error("invalid_kind", "kind", "Fact kind must be 'entity' or 'relation'."))
    if fact.get("ontology_id") != definition.get("id"):
        errors.append(_error("ontology_mismatch", "ontology_id", "Fact ontology_id does not match the definition."))
    if fact.get("ontology_version") != definition.get("version"):
        errors.append(_error("ontology_version_mismatch", "ontology_version", "Fact ontology_version does not match the definition."))
    workspace = fact.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        errors.append(_error("invalid_workspace", "workspace", "Fact workspace must be a nonempty string."))

    provenance = fact.get("provenance")
    if not isinstance(provenance, list) or not provenance:
        errors.append(_error("missing_provenance", "provenance", "At least one provenance record is required."))
    else:
        for index, record in enumerate(provenance):
            field = f"provenance.{index}"
            if not isinstance(record, dict):
                errors.append(_error("invalid_provenance", field, "Provenance entries must be mappings."))
                continue
            for key in ("document_id", "source_id"):
                if not _valid_identifier(record.get(key)):
                    errors.append(_error("invalid_provenance", f"{field}.{key}", f"Provenance {key} must be a nonempty identifier."))
            if "confidence" in record:
                confidence = record["confidence"]
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    errors.append(_error("invalid_confidence", f"{field}.confidence", "Confidence must be a finite number from 0 to 1."))

    properties = fact.get("properties", {})
    if not isinstance(properties, dict):
        errors.append(_error("invalid_properties", "properties", "Fact properties must be a mapping."))
        properties = {}

    if kind == "entity":
        entity_type = fact.get("entity_type")
        entity_definition = definition.get("entities", {}).get(entity_type) if isinstance(entity_type, str) and isinstance(definition.get("entities"), dict) else None
        if entity_definition is None:
            errors.append(_error("unknown_entity_type", "entity_type", f"Unknown entity type {entity_type!r}."))
        else:
            schema, _ = _property_json_schema(entity_definition.get("properties", {}))
            for error in sorted(jsonschema.Draft202012Validator(schema).iter_errors(properties), key=lambda item: (_path(item.absolute_path), item.validator or "")):
                field_parts = ["properties", *error.absolute_path]
                if error.validator == "required":
                    missing = next((name for name in error.validator_value if name not in error.instance), None)
                    if missing is not None:
                        field_parts.append(missing)
                elif error.validator == "additionalProperties":
                    unexpected = next((name for name in error.instance if name not in error.schema.get("properties", {})), None)
                    if unexpected is not None:
                        field_parts.append(str(unexpected))
                errors.append(_error("invalid_property", _path(field_parts), error.message))

    elif kind == "relation":
        predicate = fact.get("predicate")
        relation = definition.get("relations", {}).get(predicate) if isinstance(predicate, str) and isinstance(definition.get("relations"), dict) else None
        if relation is None:
            errors.append(_error("unknown_relation_type", "predicate", f"Unknown relation type {predicate!r}."))
        else:
            subject_id, object_id = fact.get("subject_id"), fact.get("object_id")
            for field, endpoint_id, allowed_key in (("subject_id", subject_id, "source"), ("object_id", object_id, "target")):
                if not _valid_identifier(endpoint_id):
                    errors.append(_error("invalid_identifier", field, f"{field} must be a nonempty stable identifier."))
                    continue
                endpoint = entities.get(endpoint_id)
                if endpoint is None:
                    errors.append(_error("missing_endpoint", field, f"Endpoint {endpoint_id!r} does not exist."))
                    continue
                endpoint_type, endpoint_workspace, endpoint_ontology_id, endpoint_version = _entity_info(endpoint)
                if endpoint_workspace != workspace:
                    errors.append(_error("workspace_mismatch", field, f"Endpoint {endpoint_id!r} belongs to a different workspace."))
                if endpoint_ontology_id != definition.get("id"):
                    errors.append(_error("ontology_mismatch", field, f"Endpoint {endpoint_id!r} belongs to a different ontology."))
                if endpoint_version != fact.get("ontology_version"):
                    errors.append(_error("ontology_version_mismatch", field, f"Endpoint {endpoint_id!r} uses a different ontology version."))
                allowed_types = _relation_types(relation.get(allowed_key, []))
                if endpoint_type not in allowed_types:
                    errors.append(_error("endpoint_type_mismatch", field, f"Endpoint type {endpoint_type!r} is not allowed for {predicate}.{allowed_key}."))
            if subject_id == object_id and relation.get("constraints", {}).get("allow_self_reference", True) is False:
                errors.append(_error("self_reference", "object_id", f"Relation {predicate} does not allow self-reference."))
            if properties:
                errors.append(_error("unexpected_properties", "properties", "This relation type does not define properties."))
            constraints = relation.get("constraints", {})
            if isinstance(constraints, dict):
                for limit_key, endpoint_key, endpoint_id in (("source_max", "subject_id", subject_id), ("target_max", "object_id", object_id)):
                    limit = constraints.get(limit_key)
                    if isinstance(limit, int) and not isinstance(limit, bool) and endpoint_id:
                        count = sum(
                            1 for existing in facts
                            if isinstance(existing, dict)
                            and existing.get("id") != fact_id
                            and existing.get("kind") == "relation"
                            and existing.get("predicate") == predicate
                            and existing.get(endpoint_key) == endpoint_id
                            and existing.get("workspace") == workspace
                        )
                        if count >= limit:
                            errors.append(_error("cardinality_exceeded", endpoint_key, f"Relation {predicate} exceeds {limit_key}={limit} for endpoint {endpoint_id!r}."))

    return errors
