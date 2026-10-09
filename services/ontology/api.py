"""Workspace-scoped ontology control plane. Production always uses PostgreSQL."""
import hmac
import json
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import migration
from .validation import load_definition, validate_fact


def create_app(store, tokens: dict, workspace: str) -> FastAPI:
    if not tokens or not workspace:
        raise ValueError("Tokens and a LightRAG workspace must be configured")
    for token, principal in tokens.items():
        if len(token) < 32 or principal.get("role") not in {"ontology_reader", "ontology_admin"}:
            raise ValueError("Each token needs 32 characters and a valid role")
        if not isinstance(principal.get("workspaces"), list) or not principal["workspaces"]:
            raise ValueError("Each token needs explicit workspace grants")

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield

    app = FastAPI(title="Open Graph RAG Ontology", version="1.0.0", lifespan=lifespan)

    def submitted_fact(body):
        fact = body.get("fact", body)
        if not isinstance(fact, dict):
            raise HTTPException(422, "fact must be an object")
        for field in ("id", "ontology_id", "ontology_version", "workspace"):
            if not isinstance(fact.get(field), str) or not fact[field]:
                raise HTTPException(422, f"fact.{field} must be a nonempty string")
        return fact

    def field(body, name):
        if name not in body:
            raise HTTPException(422, f"Missing field: {name}")
        if name in {"version", "from_version", "plan_id", "migration_id"} and (not isinstance(body[name], str) or not body[name]):
            raise HTTPException(422, f"{name} must be a nonempty string")
        return body[name]

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1_048_576:
                    return JSONResponse(status_code=413, content={"detail": "Request exceeds 1 MiB"})
            request._body = bytes(body)
        return await call_next(request)

    def reader(request: Request):
        authorization = request.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        principal = next((value for key, value in tokens.items() if hmac.compare_digest(key.encode(), supplied.encode())), None)
        if principal is None:
            raise HTTPException(401, "Invalid bearer token")
        selected = request.headers.get("x-workspace", "")
        if selected != workspace or selected not in principal["workspaces"]:
            raise HTTPException(403, "Workspace access denied")
        return {**principal, "workspace": selected, "actor": principal.get("actor", principal["role"])}

    def admin(principal=Depends(reader)):
        if principal["role"] != "ontology_admin":
            raise HTTPException(403, "ontology_admin role required")
        return principal

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse(status_code=409, content={"detail": str(error), "errors": getattr(error, "errors", [])})

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @app.get("/health")
    def health():
        store.list_ontologies(workspace)
        return {"status": "ok"}

    @app.get("/v1/ontologies")
    def ontologies(p=Depends(reader)):
        return store.list_ontologies(p["workspace"])

    @app.post("/v1/ontologies", status_code=201)
    def draft(body: dict, p=Depends(admin)):
        return store.save_draft(p["workspace"], load_definition(body.get("definition", body)), p["actor"])

    @app.get("/v1/ontologies/{ontology_id}/versions")
    def versions(ontology_id: str, p=Depends(reader)):
        return store.list_versions(p["workspace"], ontology_id)

    @app.get("/v1/ontologies/{ontology_id}/versions/{version}")
    def version(ontology_id: str, version: str, p=Depends(reader)):
        found = store.get_version(p["workspace"], ontology_id, version)
        if found is None:
            raise HTTPException(404, "Ontology version not found")
        return found

    @app.post("/v1/ontologies/{ontology_id}/validate")
    def validate_definition(ontology_id: str, body: dict, p=Depends(reader)):
        definition = load_definition(body.get("definition", body))
        if definition["id"] != ontology_id:
            raise HTTPException(422, "Ontology ID mismatch")
        return {"valid": True, "definition": definition}

    @app.post("/v1/ontologies/{ontology_id}/publish")
    def publish(ontology_id: str, body: dict, p=Depends(admin)):
        return store.publish(p["workspace"], ontology_id, field(body, "version"), p["actor"])

    @app.post("/v1/facts/validate")
    def validate(body: dict, p=Depends(reader)):
        fact = submitted_fact(body)
        if fact.get("workspace") != p["workspace"]:
            raise HTTPException(403, "Fact workspace mismatch")
        definition = store.get_version(p["workspace"], fact["ontology_id"], fact["ontology_version"])
        if definition is None:
            raise HTTPException(404, "Ontology version not found")
        facts = store.list_facts(p["workspace"], fact["ontology_id"])
        entities = {f["id"]: f for f in facts if f["kind"] == "entity"}
        errors = validate_fact(definition, fact, entities, facts)
        return {"valid": not errors, "errors": errors}

    @app.post("/v1/facts", status_code=201)
    def write(body: dict, p=Depends(admin)):
        fact = submitted_fact(body)
        if fact.get("workspace") != p["workspace"]:
            raise HTTPException(403, "Fact workspace mismatch")
        mode = body.get("mode", "enforce")
        if mode not in {"observe", "quarantine", "enforce"}:
            raise HTTPException(422, "Unknown enforcement mode")
        return store.write_fact(p["workspace"], fact, mode=mode, actor=p["actor"])

    @app.get("/v1/facts")
    def facts(p=Depends(reader)):
        return store.list_facts(p["workspace"])

    @app.get("/v1/quarantine")
    def quarantine(p=Depends(reader)):
        return store.list_quarantine(p["workspace"])

    @app.get("/v1/projection-status")
    def sync(p=Depends(reader)):
        return store.list_sync(p["workspace"])

    @app.get("/v1/ontology-audit")
    def audit(p=Depends(reader)):
        return store.audit(p["workspace"])

    @app.get("/v1/ontologies/{ontology_id}/versions/{version}/extraction-profile")
    def profile(ontology_id: str, version: str, p=Depends(reader)):
        definition = store.get_version(p["workspace"], ontology_id, version)
        if definition is None:
            raise HTTPException(404, "Ontology version not found")
        from .adapters.lightrag import extraction_guidance
        return extraction_guidance(definition)

    @app.post("/v1/ontologies/{ontology_id}/migrations/plan")
    def plan(ontology_id: str, body: dict, p=Depends(admin)):
        target = load_definition(field(body, "definition"))
        if target["id"] != ontology_id:
            raise HTTPException(422, "Ontology ID mismatch")
        if not isinstance(body.get("renames", {}), dict):
            raise HTTPException(422, "renames must be an object")
        return migration.plan_migration(store, p["workspace"], ontology_id, field(body, "from_version"), target, body.get("renames", {}), p["actor"])

    @app.post("/v1/ontologies/{ontology_id}/migrations/apply")
    def apply(ontology_id: str, body: dict, p=Depends(admin)):
        return migration.apply_migration(store, p["workspace"], field(body, "plan_id"), p["actor"], ontology_id=ontology_id)

    @app.post("/v1/ontologies/{ontology_id}/migrations/rollback")
    def rollback(ontology_id: str, body: dict, p=Depends(admin)):
        return migration.rollback_migration(store, p["workspace"], field(body, "migration_id"), p["actor"], ontology_id=ontology_id)

    return app


def production_app():
    from .store import Store
    return create_app(Store(os.environ["ONTOLOGY_DATABASE_URL"]), json.loads(os.environ["ONTOLOGY_TOKENS"]), os.environ["LIGHTRAG_WORKSPACE"])
