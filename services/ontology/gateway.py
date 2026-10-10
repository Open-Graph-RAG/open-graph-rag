"""A retrieval-only proxy in front of the private LightRAG API."""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Iterable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


DEFAULT_ROUTES: dict[str, frozenset[str]] = {
    "/query/data": frozenset({"POST"}),
    "/query": frozenset({"POST"}),
    "/graphs": frozenset({"GET"}),
    "/graph/label/list": frozenset({"GET"}),
    "/graph/label/popular": frozenset({"GET"}),
    "/graph/label/search": frozenset({"GET"}),
    "/graph/entity/exists": frozenset({"GET"}),
}


def create_gateway_app(
    lightrag_url: str,
    lightrag_api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
    allowed_routes: dict[str, Iterable[str]] | None = None,
    max_request_bytes: int = 1_048_576,
) -> FastAPI:
    """Build a gateway that only exposes health and named retrieval routes.

    Callers present the configured key in ``X-API-Key``. The gateway checks
    that key and overwrites it upstream, so clients cannot choose upstream
    credentials or URL paths. It has no document-ingestion or graph-write
    routes; the trusted ontology projector talks to LightRAG on its private
    backend network.
    """
    if not lightrag_url or not lightrag_url.strip():
        raise ValueError("LightRAG URL is required")
    if not lightrag_api_key or not lightrag_api_key.strip():
        raise ValueError("LightRAG API key is required")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.lightrag_client is None:
            app.state.lightrag_client = httpx.AsyncClient(timeout=60.0)
        try:
            yield
        finally:
            if app.state.owns_client and app.state.lightrag_client is not None:
                await app.state.lightrag_client.aclose()

    app = FastAPI(title="LightRAG Retrieval Gateway", docs_url=None, redoc_url=None, lifespan=lifespan)
    routes = {
        path: frozenset(method.upper() for method in methods)
        for path, methods in (allowed_routes or DEFAULT_ROUTES).items()
    }
    base_url = lightrag_url.rstrip("/")
    app.state.lightrag_client = client
    app.state.owns_client = client is None

    async def upstream_get(path: str, params: dict[str, str] | None = None) -> Response:
        upstream_client = app.state.lightrag_client
        try:
            upstream = await upstream_client.get(
                f"{base_url}{path}", params=params,
                headers={"X-API-Key": lightrag_api_key},
            )
        except httpx.HTTPError:
            return JSONResponse({"detail": "LightRAG is unavailable"}, status_code=502)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={"content-type": upstream.headers.get("content-type", "application/json")},
        )

    @app.get("/health")
    async def health() -> Response:
        return await upstream_get("/health")

    @app.api_route("/{requested_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def proxy(request: Request, requested_path: str) -> Response:
        path = "/" + requested_path
        method = request.method.upper()
        if method not in routes.get(path, frozenset()):
            return JSONResponse({"detail": "route or method is not available through this gateway"}, status_code=404)
        supplied_key = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied_key.encode("utf-8"), lightrag_api_key.encode("utf-8")):
            return JSONResponse({"detail": "invalid API key"}, status_code=401)
        body = b""
        if method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > max_request_bytes:
                return JSONResponse({"detail": "request body is too large"}, status_code=413)
            bounded_body = bytearray()
            async for chunk in request.stream():
                bounded_body.extend(chunk)
                if len(bounded_body) > max_request_bytes:
                    return JSONResponse({"detail": "request body is too large"}, status_code=413)
            body = bytes(bounded_body)
        upstream_client = app.state.lightrag_client
        try:
            upstream = await upstream_client.request(
                method,
                f"{base_url}{path}",
                params=list(request.query_params.multi_items()),
                content=body,
                headers={
                    "X-API-Key": lightrag_api_key,
                    "Accept": request.headers.get("accept", "application/json"),
                    **({"Content-Type": request.headers["content-type"]} if "content-type" in request.headers else {}),
                },
            )
        except httpx.HTTPError:
            return JSONResponse({"detail": "LightRAG is unavailable"}, status_code=502)
        response_headers = {}
        for name in ("content-type", "content-disposition"):
            if name in upstream.headers:
                response_headers[name] = upstream.headers[name]
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)

    return app


def production_app() -> FastAPI:
    """Uvicorn factory for the governed compose profile."""
    return create_gateway_app(os.environ["LIGHTRAG_URL"], os.environ["LIGHTRAG_API_KEY"])
