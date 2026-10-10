# Authoring an ontology

Keep definitions in version control and validate them before publication. Start with a draft and choose a stable ontology ID. A version identifies a specific, immutable contract; publish a new SemVer version for every change after publication.

```yaml
id: ogr-core
version: 1.0.0
status: draft
entities:
  System:
    description: Software or infrastructure system
    properties:
      name:
        type: string
        required: true
      lifecycle:
        type: string
        enum: [planned, active, deprecated, retired]
  Decision:
    description: Recorded architecture or business decision
    properties:
      title:
        type: string
        required: true
relations:
  DEPENDS_ON:
    source: [System]
    target: [System]
    directed: true
validation:
  unknown_entity_type: reject
  unknown_relation_type: reject
  missing_required_property: reject
```

Use relation names as stable predicates. Declare source and target types explicitly; a directed relation is serialized in canonical facts as `subject_id`, `predicate`, `object_id`. Do not encode direction by relying on graph edge order. Add required properties only when all accepted facts can provide them. Unknown values should be rejected or quarantined by the fact validation workflow, not silently added to the published vocabulary.

The API endpoints `/v1/ontologies/{id}/validate` and `/v1/ontologies/{id}/publish` validate the submitted definition and publish a version. `/v1/facts/validate` checks a fact without storing it; `POST /v1/facts` accepts only a valid fact referencing a published version and creates its outbox event atomically. See the OpenAPI schema served by the ontology service for exact request fields.

Extraction guidance can be generated from entity and relation descriptions and attached to an extraction profile. Treat it as a prompt aid only. The current LightRAG API has no verified v1.5.7 hook that applies the profile and deterministically validates every ingested document candidate. Acceptance into the canonical fact store is the governance boundary.
