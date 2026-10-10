"""Dry-run and transactional ontology migration helpers."""
from __future__ import annotations

from copy import deepcopy
import uuid
from typing import Any, Mapping

from services.ontology.store import Store, content_hash, _connection


def _renamed_fact(fact: Mapping[str, Any], renames: Mapping[str, Mapping[str, str]], version: str,
                  definition: Mapping[str, Any] | None = None) -> dict:
    result = deepcopy(dict(fact))
    if result.get("kind") == "entity":
        entity_type = result.get("entity_type")
        if isinstance(entity_type, str):
            result["entity_type"] = renames.get("entities", {}).get(entity_type, entity_type)
        property_updates = renames.get("fact_properties", {}).get(result.get("id"))
        if property_updates is not None:
            result["properties"] = deepcopy(dict(property_updates))
    elif result.get("kind") == "relation":
        predicate = result.get("predicate")
        if isinstance(predicate, str):
            result["predicate"] = renames.get("relations", {}).get(predicate, predicate)
        result.pop("directed", None)
        predicate = result.get("predicate")
        relation = (definition or {}).get("relations", {}).get(predicate) if isinstance(predicate, str) else None
        if relation:
            result["directed"] = relation["directed"]
    result["ontology_version"] = version
    return result


def _definition_diff(source: Mapping[str, Any], target: Mapping[str, Any], renames: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    changes = []
    for category in ("entities", "relations"):
        before, after = source.get(category, {}), target.get(category, {})
        mapped = renames.get(category, {})
        for name in sorted(before):
            dest = mapped.get(name, name)
            if dest not in after:
                changes.append({"category": category, "name": name, "change": "removed", "breaking": True})
            elif dest != name:
                changes.append({"category": category, "name": name, "change": "renamed", "to": dest, "breaking": True})
            elif category == "relations":
                old_rel, new_rel = before[name], after[dest]
                changed = [key for key in ("source", "target", "directed", "constraints") if old_rel.get(key) != new_rel.get(key)]
                if changed:
                    changes.append({"category": category, "name": name, "change": "modified", "fields": changed, "breaking": True})
                elif old_rel.get("description") != new_rel.get("description"):
                    changes.append({"category": category, "name": name, "change": "description_changed", "breaking": False})
            else:
                old_entity, new_entity = before[name], after[dest]
                if old_entity.get("description") != new_entity.get("description"):
                    changes.append({"category": category, "name": name, "change": "description_changed", "breaking": False})
                old_props, new_props = old_entity.get("properties", {}), new_entity.get("properties", {})
                for prop in sorted(set(old_props) - set(new_props)):
                    changes.append({"category": "property", "entity": name, "name": prop, "change": "removed", "breaking": True})
                for prop in sorted(set(new_props) - set(old_props)):
                    required = bool(new_props[prop].get("required"))
                    changes.append({"category": "property", "entity": name, "name": prop, "change": "added", "breaking": required})
                for prop in sorted(set(old_props) & set(new_props)):
                    old_prop, new_prop = old_props[prop], new_props[prop]
                    old_contract = {k: v for k, v in old_prop.items() if k != "description"}
                    new_contract = {k: v for k, v in new_prop.items() if k != "description"}
                    if old_contract != new_contract:
                        required_tightened = not old_prop.get("required") and new_prop.get("required")
                        # Any contract change can invalidate existing facts; classify conservatively.
                        changes.append({"category": "property", "entity": name, "name": prop,
                                        "change": "tightened" if required_tightened else "modified", "breaking": True})
        for name in sorted(set(after) - set(before) - set(mapped.values())):
            changes.append({"category": category, "name": name, "change": "added", "breaking": False})
    return {"breaking": any(item["breaking"] for item in changes), "changes": changes}


def _snapshot_hash(facts: list[dict]) -> str:
    return content_hash({"facts": sorted(facts, key=lambda fact: fact.get("id", ""))})


def plan_migration(store: Store, workspace: str, ontology_id: str, from_version: str,
                   to_definition: Mapping[str, Any], renames: Mapping[str, Mapping[str, str]], actor: str) -> dict[str, Any]:
    """Validate a complete transformed snapshot without changing governed facts."""
    from services.ontology.validation import load_definition, validate_fact
    target = load_definition(dict(to_definition))
    if target.get("id") != ontology_id:
        raise ValueError("target ontology id must match ontology_id")
    if not isinstance(renames, Mapping):
        raise ValueError("renames must be an object")
    if set(renames) - {"entities", "relations", "fact_properties"}:
        raise ValueError("renames may only map entities, relations, and fact_properties")
    for group, mapping in renames.items():
        if group == "fact_properties":
            if not isinstance(mapping, Mapping) or any(not isinstance(k, str) or not isinstance(v, Mapping) for k, v in mapping.items()):
                raise ValueError("fact_properties must map fact IDs to complete property objects")
            continue
        if not isinstance(mapping, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
            raise ValueError(f"{group} rename map must contain string names")
    current = store.get_version(workspace, ontology_id, from_version)
    if current is None:
        raise KeyError(f"unknown source ontology version {ontology_id}@{from_version}")
    target_hash = content_hash(target)
    facts = store.list_facts(workspace, ontology_id)
    if current.get("status") != "published":
        raise ValueError("migration source version must be published")
    if not isinstance(store, Store) and store._active_versions.get((workspace, ontology_id)) != from_version:
        raise ValueError("migration source version must be active")
    fact_ids = {fact.get("id") for fact in facts if fact.get("kind") == "entity"}
    errors = [{"fact_id": fact_id, "errors": [{"code": "unknown_fact", "field": f"renames.fact_properties.{fact_id}", "message": "Property updates must target an existing entity fact."}]}
              for fact_id in sorted(set(renames.get("fact_properties", {})) - fact_ids)]
    proposed = [_renamed_fact(f, renames, target["version"], target) for f in facts]
    entities = {f["id"]: f for f in proposed if f.get("kind") == "entity"}
    for fact in facts:
        if fact.get("ontology_version") != from_version:
            errors.append({"fact_id": fact.get("id"), "errors": [{"code": "source_version_mismatch", "field": "ontology_version", "message": f"Fact belongs to {fact.get('ontology_version')!r}, expected {from_version!r}."}]})
    for fact in proposed:
        others = [item for item in proposed if item.get("id") != fact.get("id")]
        available_entities = {key: value for key, value in entities.items() if key != fact.get("id")}
        found = validate_fact(target, fact, available_entities, others)
        if found:
            errors.append({"fact_id": fact.get("id"), "errors": found})
    plan_id = str(uuid.uuid4())
    source_snapshot = _snapshot_hash(facts)
    plan = {"id": plan_id, "workspace": workspace, "ontology_id": ontology_id,
            "from_version": from_version, "to_version": target["version"],
            "target_hash": target_hash, "source_snapshot": source_snapshot,
            "facts": len(facts), "renames": deepcopy(dict(renames)),
            "diff": _definition_diff(current, target, renames), "valid": not errors,
            "errors": errors, "state": "ready" if not errors else "blocked"}
    if not isinstance(store, Store):
        store._plans[(workspace, plan_id)] = {"plan": deepcopy(plan), "target": deepcopy(target)}
        store.audit(workspace)  # retain API parity; append the event below
        store._audits.append({"workspace": workspace, "actor": actor, "action": "ontology.migration_planned", "subject": plan_id, "details": deepcopy(plan)})
        return plan
    from psycopg.types.json import Jsonb
    with _connection(store.database_url) as conn, conn.cursor() as cur:
        store._lock(cur, workspace, ontology_id)
        cur.execute("SELECT active_version FROM ontology.ontology WHERE workspace=%s AND id=%s", (workspace, ontology_id))
        active = cur.fetchone()
        if not active or active["active_version"] != from_version:
            raise ValueError("migration source version is no longer active; retry dry-run")
        # Compare complete records under the same lock used by all canonical writes.
        cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s AND ontology_id=%s ORDER BY id", (workspace, ontology_id))
        locked_facts = [dict(item["record"]) for item in cur.fetchall()]
        if _snapshot_hash(locked_facts) != source_snapshot:
            raise ValueError("facts changed while migration was being planned; retry dry-run")
        cur.execute("INSERT INTO ontology.migration_plan(workspace,id,ontology_id,from_version,target_definition,target_hash,renames,plan,state,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (workspace, plan_id, ontology_id, from_version, Jsonb(target), target_hash, Jsonb(dict(renames)), Jsonb(plan), plan["state"], actor))
        store._audit(cur, workspace, actor, "ontology.migration_planned", plan_id, plan)
    return plan


def apply_migration(store: Store, workspace: str, plan_id: str, actor: str,
                    ontology_id: str | None = None) -> dict[str, Any]:
    """Apply a dry-run plan atomically, saving complete pre-change fact snapshots."""
    from services.ontology.validation import validate_fact
    if not isinstance(store, Store):
        entry = store._plans.get((workspace, plan_id))
        if not entry: raise KeyError(plan_id)
        plan, target = entry["plan"], entry["target"]
        if ontology_id is not None and ontology_id != plan["ontology_id"]: raise ValueError("migration plan does not belong to requested ontology")
        if plan["state"] != "ready": raise ValueError(f"migration plan is {plan['state']}")
        if store._active_versions.get((workspace, plan["ontology_id"])) != plan["from_version"]:
            raise ValueError("migration source version is no longer active")
        old_facts = store.list_facts(workspace, plan["ontology_id"])
        if _snapshot_hash(old_facts) != plan["source_snapshot"]:
            raise ValueError("canonical facts changed after dry-run")
        transformed = [_renamed_fact(f, plan["renames"], target["version"], target) for f in old_facts]
        entities = {f["id"]: f for f in transformed if f.get("kind") == "entity"}
        for fact in transformed:
            available_entities = {key: value for key, value in entities.items() if key != fact.get("id")}
            errors = validate_fact(target, fact, entities, transformed, check_uniqueness=False)
            if errors: raise ValueError({"migration_invalid": [{"fact_id": fact["id"], "errors": errors}]})
        store._snapshots[(workspace, plan_id)] = deepcopy(old_facts)
        version_key = (workspace, plan["ontology_id"], target["version"])
        existing_target = store._versions.get(version_key)
        if existing_target and (existing_target["status"] != "published" or existing_target["content_hash"] != content_hash(target)):
            raise ValueError("target ontology version already exists with different content")
        if not existing_target:
            store._versions[version_key] = {
                "definition": deepcopy(target), "version": target["version"], "status": "published", "content_hash": content_hash(target)}
        for fact in transformed:
            store._facts[(workspace, fact["id"])] = deepcopy(fact)
            for outbox in store._outbox:
                if outbox["workspace"] == workspace and outbox["fact"]["id"] == fact["id"]:
                    outbox["fact"] = deepcopy(fact); outbox["delivered"] = False; outbox["leased"] = False; outbox["lease_token"] = None
        store._applied_snapshots[(workspace, plan_id)] = deepcopy(transformed)
        store._active_versions[(workspace, plan["ontology_id"])] = target["version"]
        plan["state"] = "applied"
        store._audits.append({"workspace": workspace, "actor": actor, "action": "ontology.migration_applied", "subject": plan_id})
        return deepcopy(plan)
    from psycopg.types.json import Jsonb
    with _connection(store.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM ontology.migration_plan WHERE workspace=%s AND id=%s FOR UPDATE", (workspace, plan_id))
        row = cur.fetchone()
        if not row: raise KeyError(plan_id)
        if row["state"] != "ready": raise ValueError(f"migration plan is {row['state']}")
        if ontology_id is not None and ontology_id != row["ontology_id"]:
            raise ValueError("migration plan does not belong to requested ontology")
        ontology_id = row["ontology_id"]
        store._lock(cur, workspace, ontology_id)
        cur.execute("SELECT active_version FROM ontology.ontology WHERE workspace=%s AND id=%s FOR UPDATE", (workspace, ontology_id))
        active = cur.fetchone()
        if not active or active["active_version"] != row["from_version"]:
            raise ValueError("migration source version is no longer active")
        cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s AND ontology_id=%s ORDER BY id FOR UPDATE", (workspace, ontology_id))
        old_facts = [dict(x["record"]) for x in cur.fetchall()]
        cur.execute("SELECT status,content_hash FROM ontology.ontology_version WHERE workspace=%s AND ontology_id=%s AND version=%s FOR UPDATE", (workspace, ontology_id, row["plan"]["to_version"]))
        target_row = cur.fetchone()
        target = dict(row["target_definition"])
        if target_row and target_row["status"] == "published" and target_row["content_hash"] == row["target_hash"]:
            cur.execute("SELECT definition FROM ontology.ontology_version WHERE workspace=%s AND ontology_id=%s AND version=%s", (workspace, ontology_id, row["plan"]["to_version"]))
            target = dict(cur.fetchone()["definition"])
        elif target_row:
            raise ValueError("target ontology version already exists with different content")
        transformed = [_renamed_fact(f, dict(row["renames"]), target["version"], target) for f in old_facts]
        entities = {f["id"]: f for f in transformed if f.get("kind") == "entity"}
        for fact in transformed:
            available_entities = {key: value for key, value in entities.items() if key != fact.get("id")}
            errors = validate_fact(target, fact, entities, transformed, check_uniqueness=False)
            if errors: raise ValueError({"migration_invalid": [{"fact_id": fact["id"], "errors": errors}]})
        actual_hash = content_hash(target)
        if actual_hash != row["target_hash"]: raise ValueError("migration target definition changed after dry-run")
        cur.execute("SELECT count(*) AS n FROM ontology.fact WHERE workspace=%s AND ontology_id=%s", (workspace, ontology_id))
        if cur.fetchone()["n"] != row["plan"]["facts"]:
            raise ValueError("canonical fact set changed after dry-run")
        if _snapshot_hash(old_facts) != row["plan"]["source_snapshot"]:
            raise ValueError("canonical facts changed after dry-run")
        if not target_row:
            cur.execute("INSERT INTO ontology.ontology_version(workspace,ontology_id,version,status,definition,content_hash,created_by,published_by,published_at) VALUES (%s,%s,%s,'published',%s,%s,%s,%s,now())",
                        (workspace, ontology_id, target["version"], Jsonb(target), actual_hash, actor, actor))
        target["status"] = "published"
        for old in old_facts:
            cur.execute("INSERT INTO ontology.migration_snapshot(workspace,migration_id,fact_id,record) VALUES (%s,%s,%s,%s)", (workspace, plan_id, old["id"], Jsonb(old)))
        for fact in transformed:
            cur.execute("UPDATE ontology.fact SET ontology_version=%s,record=%s WHERE workspace=%s AND id=%s",
                        (target["version"], Jsonb(fact), workspace, fact["id"]))
            cur.execute("UPDATE ontology.outbox SET payload=%s,available_at=now(),delivered_at=NULL,leased_until=NULL,lease_token=NULL WHERE workspace=%s AND fact_id=%s",
                        (Jsonb(fact), workspace, fact["id"]))
            cur.execute("UPDATE ontology.migration_snapshot SET applied_record=%s WHERE workspace=%s AND migration_id=%s AND fact_id=%s",
                        (Jsonb(fact), workspace, plan_id, fact["id"]))
        cur.execute("UPDATE ontology.migration_plan SET state='applied',applied_at=now() WHERE workspace=%s AND id=%s", (workspace, plan_id))
        cur.execute("UPDATE ontology.ontology SET active_version=%s,updated_at=now() WHERE workspace=%s AND id=%s",
                    (target["version"], workspace, ontology_id))
        store._audit(cur, workspace, actor, "ontology.migration_applied", plan_id, {"facts": len(transformed), "to_version": target["version"]})
    return {**dict(row["plan"]), "state": "applied"}


def rollback_migration(store: Store, workspace: str, migration_id: str, actor: str,
                       ontology_id: str | None = None) -> dict[str, Any]:
    """Restore captured records atomically; the published target remains immutable."""
    if not isinstance(store, Store):
        entry = store._plans.get((workspace, migration_id))
        snapshots = store._snapshots.get((workspace, migration_id))
        if not entry: raise KeyError(migration_id)
        plan = entry["plan"]
        if ontology_id is not None and ontology_id != plan["ontology_id"]: raise ValueError("migration does not belong to requested ontology")
        if plan["state"] != "applied": raise ValueError("only an applied migration can be rolled back")
        if store._active_versions.get((workspace, plan["ontology_id"])) != plan["to_version"]:
            raise ValueError("active ontology version changed after migration")
        if snapshots is None or len(snapshots) != plan["facts"]: raise RuntimeError("migration snapshot is incomplete")
        applied = store._applied_snapshots.get((workspace, migration_id))
        current = store.list_facts(workspace, plan["ontology_id"])
        if applied is None or content_hash({"facts": sorted(current, key=lambda f: f.get("id", ""))}) != content_hash({"facts": sorted(applied, key=lambda f: f.get("id", ""))}):
            raise ValueError("canonical facts changed after migration; rollback would overwrite newer writes")
        for fact in snapshots:
            store._facts[(workspace, fact["id"])] = deepcopy(fact)
            for outbox in store._outbox:
                if outbox["workspace"] == workspace and outbox["fact"]["id"] == fact["id"]:
                    outbox["fact"] = deepcopy(fact); outbox["delivered"] = False; outbox["leased"] = False; outbox["lease_token"] = None
        store._active_versions[(workspace, plan["ontology_id"])] = plan["from_version"]
        plan["state"] = "rolled_back"
        store._audits.append({"workspace": workspace, "actor": actor, "action": "ontology.migration_rolled_back", "subject": migration_id})
        return {"id": migration_id, "state": "rolled_back", "facts": len(snapshots)}
    from psycopg.types.json import Jsonb
    with _connection(store.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM ontology.migration_plan WHERE workspace=%s AND id=%s FOR UPDATE", (workspace, migration_id))
        row = cur.fetchone()
        if not row: raise KeyError(migration_id)
        if ontology_id is not None and ontology_id != row["ontology_id"]:
            raise ValueError("migration does not belong to requested ontology")
        if row["state"] != "applied": raise ValueError("only an applied migration can be rolled back")
        store._lock(cur, workspace, row["ontology_id"])
        cur.execute("SELECT active_version FROM ontology.ontology WHERE workspace=%s AND id=%s FOR UPDATE", (workspace, row["ontology_id"]))
        active = cur.fetchone()
        if not active or active["active_version"] != row["plan"]["to_version"]:
            raise ValueError("active ontology version changed after migration")
        cur.execute("SELECT fact_id,record,applied_record FROM ontology.migration_snapshot WHERE workspace=%s AND migration_id=%s FOR UPDATE", (workspace, migration_id))
        snapshots = cur.fetchall()
        if len(snapshots) != row["plan"]["facts"]: raise RuntimeError("migration snapshot is incomplete")
        cur.execute("SELECT record FROM ontology.fact WHERE workspace=%s AND ontology_id=%s ORDER BY id FOR UPDATE", (workspace, row["ontology_id"]))
        current = [dict(item["record"]) for item in cur.fetchall()]
        applied = [dict(item["applied_record"]) for item in snapshots]
        if any(item is None for item in applied) or content_hash({"facts": sorted(current, key=lambda f: f.get("id", ""))}) != content_hash({"facts": sorted(applied, key=lambda f: f.get("id", ""))}):
            raise ValueError("canonical facts changed after migration; rollback would overwrite newer writes")
        for snapshot in snapshots:
            fact = dict(snapshot["record"])
            cur.execute("UPDATE ontology.fact SET ontology_version=%s,record=%s WHERE workspace=%s AND id=%s",
                        (fact["ontology_version"], Jsonb(fact), workspace, snapshot["fact_id"]))
            cur.execute("UPDATE ontology.outbox SET payload=%s,available_at=now(),delivered_at=NULL,leased_until=NULL,lease_token=NULL WHERE workspace=%s AND fact_id=%s",
                        (Jsonb(fact), workspace, snapshot["fact_id"]))
        cur.execute("UPDATE ontology.migration_plan SET state='rolled_back' WHERE workspace=%s AND id=%s", (workspace, migration_id))
        cur.execute("UPDATE ontology.ontology SET active_version=%s,updated_at=now() WHERE workspace=%s AND id=%s",
                    (row["from_version"], workspace, row["ontology_id"]))
        store._audit(cur, workspace, actor, "ontology.migration_rolled_back", migration_id, {"facts": len(snapshots)})
    return {"id": migration_id, "state": "rolled_back", "facts": len(snapshots)}
