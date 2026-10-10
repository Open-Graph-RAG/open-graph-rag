"""FastAPI application for the read-only ontology operations console.

The UI server is a thin presentation layer over the existing ontology
control-plane API. It logs users in with a workspace-scoped bearer
token (stored server-side behind an opaque cookie), forwards every read to the
nine `/v1/*` endpoints, and renders the results as Jinja2 templates.
There are no write/POST endpoints besides `/login` and `/logout`; all
mutations stay on the canonical ontology API.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_303_SEE_OTHER

from . import auth
from .api_client import OntologyClient, UpstreamError
from .filters import register as register_filters
from .auth import LoginRedirect


log = logging.getLogger(__name__)


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_API_URL = "http://ontology:8010"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


SECTION_BY_PATH = {
    "/": "overview",
    "/ontologies": "ontologies",
    "/facts": "facts",
    "/quarantine": "quarantine",
    "/projection-status": "projection",
    "/audit": "audit",
}


def _current_section(path: str) -> str:
    if path in SECTION_BY_PATH:
        return SECTION_BY_PATH[path]
    if path.startswith("/ontologies"):
        return "ontologies"
    return ""


def _shape_entity_properties(properties: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Convert `{name: {type, required, ...}}` into the list-of-objects
    shape the `_partials/property_table.html` template expects."""
    if not isinstance(properties, dict):
        return []
    items: list[dict[str, Any]] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            items.append({"name": str(name), "type": "any", "required": False, "description": ""})
            continue
        items.append({
            "name": str(name),
            "type": spec.get("type", "any"),
            "required": bool(spec.get("required", False)),
            "description": spec.get("description", "") or "",
            "enum": spec.get("enum"),
        })
    return items


def _shape_relations(relations: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Convert `{PREDICATE: {source, target, directed, ...}}` into the
    list-of-objects shape the `_partials/relation_table.html` template expects."""
    if not isinstance(relations, dict):
        return []
    items: list[dict[str, Any]] = []
    for name, spec in relations.items():
        if not isinstance(spec, dict):
            continue
        source = spec.get("source")
        target = spec.get("target")
        if isinstance(source, list):
            source = ", ".join(str(s) for s in source)
        if isinstance(target, list):
            target = ", ".join(str(t) for t in target)
        items.append({
            "name": str(name),
            "source": str(source) if source is not None else "—",
            "target": str(target) if target is not None else "—",
            "directed": bool(spec.get("directed", True)),
            "description": spec.get("description", "") or "",
        })
    return items


def _shape_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize fact records to the keys the partials use."""
    out: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        out.append({
            "id": f.get("id", "—"),
            "kind": f.get("kind", "—"),
            "kind_label": f.get("kind", "—"),
            "ontology_id": f.get("ontology_id", "—"),
            "ontology_version": f.get("ontology_version", "—"),
            "subject": f.get("subject_id") or f.get("subject") or "—",
            "predicate": f.get("predicate") or "—",
            "object": f.get("object_id") or f.get("object") or "—",
            "entity_type": f.get("entity_type", ""),
            "directed": f.get("directed", False),
            "recorded_at": f.get("created_at", ""),
            "status": f.get("status", ""),
        })
    return out


def _shape_ontology(definition: dict[str, Any]) -> dict[str, Any]:
    """Convert the API's nested ontology dict into the list-of-objects
    shape the entity/relation partials expect."""
    if not isinstance(definition, dict):
        return definition
    entities = definition.get("entities", {}) or {}
    relations = definition.get("relations", {}) or {}
    entity_rows = []
    for name, spec in entities.items():
        if not isinstance(spec, dict):
            spec = {"description": "", "properties": {}}
        entity_rows.append({
            "name": str(name),
            "description": spec.get("description", "") or "",
            "properties": _shape_entity_properties(spec.get("properties", {})),
        })
    return {
        **definition,
        "entity_rows": entity_rows,
        "relation_rows": _shape_relations(relations),
    }


def _int_query(request: Request, name: str, default: int, *, maximum: int | None = None) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 1:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def _str_query(request: Request, name: str) -> str:
    raw = request.query_params.get(name, "")
    return raw.strip() if isinstance(raw, str) else ""


def _paginate(items: list[Any], page: int, page_size: int) -> tuple[list[Any], int, int, int]:
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], total, total_pages, page


def _query_args(request: Request, *, exclude: set[str]) -> dict[str, str]:
    return {k: v for k, v in request.query_params.multi_items() if k not in exclude}


def _pagination_url(path: str, query_args: dict[str, str], page: int) -> str:
    params = {**query_args, "page": str(page)}
    return f"{path}?{urlencode(params)}"


def _format_kv(detail: Any) -> str:
    """Render a Python value as a one-line summary for flash messages."""
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, default=str)
    except Exception:  # pragma: no cover
        return str(detail)


def _safe_next(value: Any, default: str = "/") -> str:
    """Sanitize a `next` redirect target.

    Accepts only same-origin absolute paths — a single leading `/`, no
    protocol-relative URLs, no backslash trickery. Anything else falls
    back to the safe default. Mitigates open-redirect via crafted
    `next` parameters on `/login`.
    """
    if not isinstance(value, str) or not value:
        return default
    if not value.startswith("/"):
        return default
    if value.startswith("//") or value.startswith("/\\"):
        return default
    if "\\" in value:
        return default
    return value


def _origin_tuple(value: str) -> tuple[str, str, int] | None:
    """Return the normalized HTTP origin for a URL, or None if invalid."""
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if (
            scheme not in {"http", "https"}
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        port = parsed.port
    except ValueError:
        return None
    default_port = 443 if scheme == "https" else 80
    return (scheme, hostname.lower(), port if port is not None else default_port)


def _is_same_origin_request(request: Request) -> bool:
    """Reject a supplied cross-origin Origin, or Referer when Origin is absent."""
    if "origin" in request.headers:
        supplied_url = request.headers["origin"]
    elif "referer" in request.headers:
        supplied_url = request.headers["referer"]
    else:
        return True
    return _origin_tuple(supplied_url) == _origin_tuple(str(request.url))


def _pagination_links(
    path: str, query_args: dict[str, str], page: int, total_pages: int
) -> dict[str, str | None]:
    """Build safe Prev/Next URLs for the pagination partial.

    Returns `{"prev_url": ..., "next_url": ...}`, each `None` if the
    respective page is out of range.
    """
    prev_url = _pagination_url(path, query_args, page - 1) if page > 1 else None
    next_url = _pagination_url(path, query_args, page + 1) if page < total_pages else None
    return {"prev_url": prev_url, "next_url": next_url}


def _error_context(
    request: Request,
    *,
    title: str,
    message: str,
    detail: Any = None,
    back: str | None = None,
) -> dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "message": message,
        "detail": detail,
        "back": back or request.url.path,
    }


def create_app(
    api_url: str,
    session_secret: str = "",
    *,
    https_only: bool = False,
    upstream_client_factory: Any | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        api_url: Base URL of the ontology control-plane API. The UI does
            not talk to the database; it only consumes `/v1/*` here.
        session_secret: Deprecated compatibility argument; opaque sessions
            are server-side and do not use a signing secret.
        https_only: When true, the session cookie is `Secure`. Default
            `False` so the same image works on local `127.0.0.1` and in
            real HTTPS deployments.
        upstream_client_factory: Optional callable returning an
            `httpx.AsyncClient` to use for every upstream request. Tests
            inject a `MockTransport` here. In production the factory is
            `None` and a shared client is built for the app lifetime.
    """
    if not api_url or not api_url.strip():
        raise ValueError("api_url is required")
    api_url = api_url.rstrip("/")

    shared_http_client: httpx.AsyncClient = (
        upstream_client_factory() if upstream_client_factory is not None else httpx.AsyncClient(timeout=10.0)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            probe = OntologyClient(
                api_url, token="startup-probe", workspace="startup-probe", client=shared_http_client
            )
            await probe.health()
        except UpstreamError as exc:
            log.warning("ontology UI startup health probe failed: %s", exc.detail)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("ontology UI startup health probe crashed: %s", exc)
        try:
            yield
        finally:
            await shared_http_client.aclose()

    app = FastAPI(
        title="Ontology Console",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.session_store = auth.SessionStore()
    app.state.https_only = https_only

    @app.middleware("http")
    async def expire_legacy_signed_cookie(request: Request, call_next):
        response = await call_next(request)
        if "session" in request.cookies:
            # Starlette's former signed credential cookie used this default name.
            response.delete_cookie("session", path="/", httponly=True,
                                   secure=https_only, samesite="strict")
        return response
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    register_filters(templates.env)
    app.state.templates = templates
    app.state.api_url = api_url
    app.state.http_client = shared_http_client

    @app.exception_handler(LoginRedirect)
    async def _login_redirect_handler(request: Request, exc: LoginRedirect):
        auth.clear_session(request, exc.response)
        return exc.response

    def _client_for(token: str, workspace: str) -> OntologyClient:
        return OntologyClient(
            api_url, token=token, workspace=workspace, client=app.state.http_client
        )

    def _login_redirect(request: Request) -> RedirectResponse:
        """Clear the session, set a flash, and return a redirect to `/login`."""
        response = _redirect(request, "/login", next_path=request.url.path)
        response.headers["location"] += "&expired=1"
        auth.clear_session(request, response)
        return response

    def render(
        request: Request,
        name: str,
        context: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        ctx: dict[str, Any] = {
            "request": request,
            "now": datetime.now(timezone.utc),
            "current_section": _current_section(request.url.path),
        }
        principal = auth.principal(request)
        if principal:
            ctx["workspace"] = principal["workspace"]
            ctx["token_handle"] = principal["token_handle"]
            ctx["authenticated"] = True
        else:
            ctx["workspace"] = ""
            ctx["token_handle"] = ""
            ctx["authenticated"] = False
        if context:
            ctx.update(context)
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def _redirect(request: Request, path: str, *, next_path: str | None = None) -> RedirectResponse:
        target = path
        if next_path:
            from urllib.parse import quote
            target = f"{path}?next={quote(next_path, safe='/')}"
        return RedirectResponse(url=target, status_code=HTTP_303_SEE_OTHER)

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            probe = _client_for("startup-probe", "startup-probe")
            data = await probe.health()
        except UpstreamError as exc:
            return JSONResponse({"status": "degraded", "detail": exc.detail}, status_code=503)
        return JSONResponse({"status": "ok", "upstream": data}, status_code=200)

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request, next: str = "/"):
        flash = "Session expired or token invalid." if request.query_params.get("expired") == "1" else None
        return render(
            request,
            "login.html",
            {
                "next": _safe_next(next),
                "error": flash,
                "workspace_hint": os.environ.get("ONTOLOGY_WORKSPACE", ""),
            },
        )

    @app.post("/login")
    async def login_post(
        request: Request,
        token: str = Form(...),
        workspace: str = Form(...),
        next: str = Form(default="/"),
    ):
        if not _is_same_origin_request(request):
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        ok, error = auth.login(token, workspace)
        if not ok:
            return render(
                request,
                "login.html",
                {
                    "next": _safe_next(next),
                    "error": error,
                    "workspace_hint": os.environ.get("ONTOLOGY_WORKSPACE", ""),
                },
                status_code=422,
            )
        # The API is the authority for token/workspace grants; never establish a
        # browser session based only on a caller-provided workspace value.
        try:
            await _client_for(token.strip(), workspace.strip()).list_ontologies()
        except UpstreamError as exc:
            denied = exc.status_code in (401, 403)
            return render(request, "login.html", {"next": _safe_next(next),
                "error": ("Token or workspace access denied by ontology API." if denied
                          else "Ontology API is temporarily unavailable. Please try again."),
                "workspace_hint": os.environ.get("ONTOLOGY_WORKSPACE", "")},
                status_code=exc.status_code if denied else 503)
        response = RedirectResponse(url=_safe_next(next), status_code=HTTP_303_SEE_OTHER)
        try:
            auth.issue_session(request, response, token.strip(), workspace.strip())
        except RuntimeError:
            return render(request, "login.html", {"next": _safe_next(next),
                "error": "The console is temporarily unable to create a session. Try again later.",
                "workspace_hint": os.environ.get("ONTOLOGY_WORKSPACE", "")}, status_code=503)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        if not _is_same_origin_request(request):
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        response = RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
        auth.clear_session(request, response)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        import asyncio

        principal = auth.get_principal(request)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            # Issue the five probes concurrently so the homepage does not
            # serialise their timeouts. Each call uses the shared httpx
            # client so connections are pooled across requests.
            ontologies, facts, outbox, audit_entries, upstream_health = await asyncio.gather(
                client.list_ontologies(),
                client.list_facts(),
                client.list_sync(),
                client.audit(),
                client.health(),
            )
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(
                    request,
                    title="Cannot reach the ontology API",
                    message=_format_kv(exc.detail),
                    back="/",
                ),
                status_code=502,
            )

        delivered = sum(1 for row in outbox if row.get("delivered_at"))
        failed = sum(1 for row in outbox if row.get("last_error") and not row.get("delivered_at"))
        pending = len(outbox) - delivered - failed
        active_versions = sum(1 for o in ontologies if o.get("active_version"))
        last_audit_at = audit_entries[-1]["created_at"] if audit_entries else None

        return render(
            request,
            "dashboard.html",
            {
                "principal": principal,
                "health": upstream_health,
                "ontology_count": len(ontologies),
                "active_versions_count": active_versions,
                "fact_count": len(facts),
                "outbox_delivered": delivered,
                "outbox_failed": failed,
                "outbox_pending": pending,
                "recent_audit": list(reversed(audit_entries[-5:])),
                "last_audit_at": last_audit_at,
            },
        )

    @app.get("/ontologies", response_class=HTMLResponse)
    async def ontologies(request: Request):
        principal = auth.get_principal(request)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            rows = await client.list_ontologies()
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(request, title="Cannot list ontologies", message=_format_kv(exc.detail)),
                status_code=exc.status_code,
            )
        return render(request, "ontologies.html", {"principal": principal, "ontologies": rows})

    @app.get("/ontologies/{ontology_id}/versions", response_class=HTMLResponse)
    async def ontology_versions(ontology_id: str, request: Request):
        principal = auth.get_principal(request)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            rows = await client.list_versions(ontology_id)
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(
                    request,
                    title=f"Cannot list versions for {ontology_id}",
                    message=_format_kv(exc.detail),
                ),
                status_code=exc.status_code,
            )
        return render(
            request,
            "ontology_versions.html",
            {"principal": principal, "ontology_id": ontology_id, "versions": rows},
        )

    @app.get("/ontologies/{ontology_id}/versions/{version}", response_class=HTMLResponse)
    async def ontology_version(ontology_id: str, version: str, request: Request):
        principal = auth.get_principal(request)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            definition = await client.get_version(ontology_id, version)
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            if exc.status_code == 404:
                return render(
                    request,
                    "_partials/error_panel.html",
                    _error_context(
                        request,
                        title="Ontology version not found",
                        message=f"{ontology_id}@{version} is not in the registry.",
                        back=f"/ontologies/{ontology_id}/versions",
                    ),
                    status_code=404,
                )
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(
                    request,
                    title="Cannot load ontology version",
                    message=_format_kv(exc.detail),
                ),
                status_code=exc.status_code,
            )
        if definition is None:
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(
                    request,
                    title="Ontology version not found",
                    message=f"{ontology_id}@{version} is not in the registry.",
                    back=f"/ontologies/{ontology_id}/versions",
                ),
                status_code=404,
            )
        return render(
            request,
            "ontology_version.html",
            {
                "principal": principal,
                "ontology_id": ontology_id,
                "version": version,
                "definition": _shape_ontology(definition),
                "extra_q": {k: v for k, v in request.query_params.multi_items() if k != "tab"},
            },
        )

    @app.get(
        "/ontologies/{ontology_id}/versions/{version}/extraction-profile",
        response_class=HTMLResponse,
    )
    async def extraction_profile_route(ontology_id: str, version: str, request: Request):
        principal = auth.get_principal(request)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            profile = await client.extraction_profile(ontology_id, version)
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            if exc.status_code == 404:
                return render(
                    request,
                    "_partials/error_panel.html",
                    _error_context(
                        request,
                        title="Ontology version not found",
                        message=f"{ontology_id}@{version} is not in the registry.",
                    ),
                    status_code=404,
                )
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(
                    request,
                    title="Cannot load extraction profile",
                    message=_format_kv(exc.detail),
                ),
                status_code=exc.status_code,
            )
        return render(
            request,
            "extraction_profile.html",
            {
                "principal": principal,
                "ontology_id": ontology_id,
                "version": version,
                "profile": profile,
            },
        )

    @app.get("/facts", response_class=HTMLResponse)
    async def facts(request: Request):
        principal = auth.get_principal(request)
        kind = _str_query(request, "kind")
        ontology_filter = _str_query(request, "ontology_id")
        predicate = _str_query(request, "predicate")
        free_text = _str_query(request, "q")
        page = _int_query(request, "page", 1)
        page_size = _int_query(request, "page_size", DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            # The current upstream `/v1/facts` route discards the
            # `ontology_id` query parameter (see `services/ontology/api.py`).
            # We still pass it for forward compatibility, and also apply
            # the filter on the response here so the UI does not silently
            # ignore the user-selected ontology.
            rows = await client.list_facts(ontology_filter or None)
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(request, title="Cannot list facts", message=_format_kv(exc.detail)),
                status_code=exc.status_code,
            )

        if kind in {"entity", "relation"}:
            rows = [r for r in rows if r.get("kind") == kind]
        if ontology_filter:
            rows = [r for r in rows if r.get("ontology_id") == ontology_filter]
        if predicate:
            needle = predicate.lower()
            rows = [r for r in rows if isinstance(r.get("predicate"), str) and needle in r["predicate"].lower()]
        if free_text:
            needle = free_text.lower()
            rows = [
                r
                for r in rows
                if needle in json.dumps(r, default=str).lower()
            ]

        page_rows, total, total_pages, current_page = _paginate(rows, page, page_size)
        query_args = _query_args(request, exclude={"page"})
        links = _pagination_links("/facts", query_args, current_page, total_pages)
        context: dict[str, Any] = {
            "principal": principal,
            "facts": _shape_facts(page_rows),
            "filters": {
                "kind": kind,
                "ontology_id": ontology_filter,
                "predicate": predicate,
                "q": free_text,
            },
            "counts": {"total": total, "filtered": len(rows)},
            "pagination": {
                "page": current_page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "path": "/facts",
                "query_args": query_args,
                "prev_url": links["prev_url"],
                "next_url": links["next_url"],
            },
        }
        # htmx requests receive just the table region, not the full page.
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request, "_partials/facts_fragment.html", context, status_code=200
            )
        return render(request, "facts.html", context)

    @app.get("/quarantine", response_class=HTMLResponse)
    async def quarantine(request: Request):
        principal = auth.get_principal(request)
        page = _int_query(request, "page", 1)
        page_size = _int_query(request, "page_size", DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            rows = await client.list_quarantine()
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(
                    request, title="Cannot list quarantined facts", message=_format_kv(exc.detail)
                ),
                status_code=exc.status_code,
            )
        page_rows, total, total_pages, current_page = _paginate(rows, page, page_size)
        query_args = _query_args(request, exclude={"page"})
        links = _pagination_links("/quarantine", query_args, current_page, total_pages)
        return render(
            request,
            "quarantine.html",
            {
                "principal": principal,
                "items": page_rows,
                "pagination": {
                    "page": current_page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": total_pages,
                    "path": "/quarantine",
                    "query_args": query_args,
                    "prev_url": links["prev_url"],
                    "next_url": links["next_url"],
                },
            },
        )

    @app.get("/projection-status", response_class=HTMLResponse)
    async def projection_status(request: Request):
        principal = auth.get_principal(request)
        page = _int_query(request, "page", 1)
        page_size = _int_query(request, "page_size", DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            rows = await client.list_sync()
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(
                    request, title="Cannot list projection status", message=_format_kv(exc.detail)
                ),
                status_code=exc.status_code,
            )
        rows = sorted(
            rows,
            key=lambda r: (bool(r.get("delivered_at")), bool(r.get("last_error")), r.get("id", 0)),
        )
        page_rows, total, total_pages, current_page = _paginate(rows, page, page_size)
        query_args = _query_args(request, exclude={"page"})
        links = _pagination_links("/projection-status", query_args, current_page, total_pages)
        return render(
            request,
            "projection.html",
            {
                "principal": principal,
                "rows": page_rows,
                "pagination": {
                    "page": current_page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": total_pages,
                    "path": "/projection-status",
                    "query_args": query_args,
                    "prev_url": links["prev_url"],
                    "next_url": links["next_url"],
                },
            },
        )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_log(request: Request):
        principal = auth.get_principal(request)
        page = _int_query(request, "page", 1)
        page_size = _int_query(request, "page_size", DEFAULT_PAGE_SIZE, maximum=MAX_PAGE_SIZE)
        try:
            client = _client_for(principal["token"], principal["workspace"])
            rows = await client.audit()
        except UpstreamError as exc:
            if exc.status_code == 401:
                return _login_redirect(request)
            return render(
                request,
                "_partials/error_panel.html",
                _error_context(request, title="Cannot read audit log", message=_format_kv(exc.detail)),
                status_code=exc.status_code,
            )
        rows.reverse()
        page_rows, total, total_pages, current_page = _paginate(rows, page, page_size)
        query_args = _query_args(request, exclude={"page"})
        links = _pagination_links("/audit", query_args, current_page, total_pages)
        return render(
            request,
            "audit.html",
            {
                "principal": principal,
                "entries": page_rows,
                "pagination": {
                    "page": current_page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": total_pages,
                    "path": "/audit",
                    "query_args": query_args,
                    "prev_url": links["prev_url"],
                    "next_url": links["next_url"],
                },
            },
        )

    return app


def production_app() -> FastAPI:
    """Uvicorn factory for the governed compose profile."""
    api_url = os.environ.get("ONTOLOGY_API_URL", DEFAULT_API_URL)
    https_only = os.environ.get("ONTOLOGY_UI_HTTPS_ONLY", "").lower() in {"1", "true", "yes"}
    return create_app(api_url, https_only=https_only)
