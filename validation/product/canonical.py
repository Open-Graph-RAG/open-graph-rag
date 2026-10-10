"""Deterministic preparation of source-grounded synthetic canonical candidates.

Does not read questions/gold, publish an ontology, accept facts or write LightRAG.
The returned records require normal ontology API validation and acceptance.
"""
from __future__ import annotations

import hashlib
import re


def prepare_candidates(corpus, workspace="validation_governed"):
    entities, relations = [], []
    ids = set()
    for source in corpus:
        sid, version, text = source["source_id"], source["version"], source["text"]
        if not isinstance(sid, str) or not version or not isinstance(text, str):
            raise ValueError("source identity, version and text required")
        if sid in ids:
            raise ValueError("duplicate source identity")
        ids.add(sid)
        provenance = [{"source_id": sid, "document_id": sid, "source_version": version, "chunk_id": source.get("evidence_id", sid + "#body")}]
        def identifier(kind, value):
            return kind + "-" + hashlib.sha256((source["scenario_id"] + "\0" + value).encode()).hexdigest()[:24]
        def record(kind, value, **fields):
            return {"id": identifier(kind, value), "kind": kind, "workspace": workspace, "ontology_id": "ogr-core", "ontology_version": "1.0.0", "provenance": provenance, **fields}
        relationships=re.findall(r"^([^\n]+) depends on ([^\n]+)\.$", text, re.MULTILINE)
        # The existing fictional Northstar development demo states this shared
        # dependency in prose rather than the generated corpus's canonical form.
        for first,second,service in re.findall(r"The suite contains ([^,]+) and ([^,]+), which both use the shared ([^.]+)\.",text):
            relationships.extend(((first.strip(),service.strip()),(second.strip(),service.strip())))
        for subject, object_name in relationships:
            for name in (subject, object_name):
                entity = record("entity", name, entity_type="System", properties={"name": name})
                if not any(x["id"] == entity["id"] for x in entities):
                    entities.append(entity)
            relations.append(record("relation", subject + " DEPENDS_ON " + object_name,
                predicate="DEPENDS_ON", subject_id=identifier("entity", subject), object_id=identifier("entity", object_name), properties={}))
        if "/decision-v" in sid:
            title = text.splitlines()[0].removeprefix("# ")
            status = "superseded" if "This decision is superseded" in text else "accepted"
            entities.append(record("entity", sid, entity_type="Decision", properties={"title": title, "status": status}))
            if "This supersedes decision-v1.md" in text:
                older = sid.rsplit("/", 1)[0] + "/decision-v1.md"
                relations.append(record("relation", sid + " SUPERSEDES " + older,
                    predicate="SUPERSEDES", subject_id=identifier("entity", sid), object_id=identifier("entity", older), properties={}))
    known = {x["id"] for x in entities}
    if len(known) != len(entities) or len({x["id"] for x in relations}) != len(relations):
        raise ValueError("duplicate fact identity")
    if any(r["subject_id"] not in known or r["object_id"] not in known for r in relations):
        raise ValueError("relation endpoint has no source-grounded entity")
    return entities + relations


def validate_provenance(records, corpus):
    sources = {s["source_id"]: s for s in corpus}
    for fact in records:
        if not fact.get("provenance"):
            raise ValueError("missing provenance")
        for p in fact["provenance"]:
            source = sources.get(p.get("source_id"))
            if source is None or p.get("source_version") != source["version"]:
                raise ValueError("unresolved source/version provenance")
            if p.get("chunk_id") != source.get("evidence_id", source["source_id"] + "#body"):
                raise ValueError("unresolved evidence locator")
    return True
