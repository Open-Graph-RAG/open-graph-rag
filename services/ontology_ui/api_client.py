"""Async HTTP client for the ontology control-plane API.

The UI server is read-only and never accepts a static API key. Each request
is bound to a per-user bearer token and workspace stored in the signed
session cookie. This client wraps the nine read endpoints the UI needs and
translates non-2xx responses into a structured `UpstreamError` so the route
layer can map them to the right user-facing panel.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx


log = logging.getLogger(__name__)


class UpstreamError(Exception):
    """A non-success response from the ontology control plane."""

    def __init__(self, status_code: int, detail: str, payload: Any = None):
        self.status_code = status_code
        self.detail = detail
        self.payload = payload
        super().__init__(f"upstream {status_code}: {detail}")

    def __repr__(self) -> str:
        return f"UpstreamError(status_code={self.status_code!r}, detail={self.detail!r})"


class OntologyClient:
    """Thin async client over the nine read endpoints the UI consumes.

    Construction is cheap; the underlying `httpx.AsyncClient` is reused
    for the lifetime of one inbound request. Use `production_client` to
    build a long-lived client during application startup.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        workspace: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        if not token or not token.strip():
            raise ValueError("token is required")
        if not workspace or not workspace.strip():
            raise ValueError("workspace is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.workspace = workspace
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def __aenter__(self) -> "OntologyClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Workspace": self.workspace,
            "Accept": "application/json",
        }

    @staticmethod
    def _coerce(resp: httpx.Response) -> Any:
        """Parse a 2xx response body.

        Returns ``None`` for an empty body. Raises ``UpstreamError(502)``
        if the body is non-empty but not valid JSON — a malformed payload
        must not silently masquerade as a successful empty list.
        """
        if not resp.content:
            return None
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise UpstreamError(502, "Upstream returned malformed JSON") from exc

    @staticmethod
    def _coerce_list(data: Any) -> list[dict[str, Any]]:
        """Coerce a parsed payload to a list, treating a non-list as upstream failure."""
        if isinstance(data, list):
            return data
        if data is None:
            return []
        raise UpstreamError(502, "Upstream returned a non-list payload")

    def _raise(self, resp: httpx.Response) -> None:
        payload: Any = None
        if resp.content:
            try:
                payload = resp.json()
            except (ValueError, json.JSONDecodeError):
                payload = resp.text
        detail = "Upstream error"
        if isinstance(payload, dict):
            raw = payload.get("detail")
            if isinstance(raw, str) and raw.strip():
                detail = raw
            elif raw is not None:
                detail = json.dumps(raw, default=str)
        elif isinstance(payload, str) and payload.strip():
            detail = payload
        raise UpstreamError(resp.status_code, detail, payload=payload)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(502, f"Upstream unavailable: {exc}") from exc
        if not (200 <= response.status_code < 300):
            self._raise(response)
        return self._coerce(response)

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self.base_url}/health", timeout=5.0
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(502, f"Upstream unavailable: {exc}") from exc
        if 200 <= response.status_code < 300:
            data = self._coerce(response)
            return data if isinstance(data, dict) else {"status": "ok"}
        self._raise(response)
        return {"status": "error"}  # pragma: no cover - _raise always raises

    async def list_ontologies(self) -> list[dict[str, Any]]:
        return self._coerce_list(await self._get("/v1/ontologies"))

    async def list_versions(self, ontology_id: str) -> list[dict[str, Any]]:
        if not ontology_id or not ontology_id.strip():
            raise ValueError("ontology_id is required")
        return self._coerce_list(await self._get(f"/v1/ontologies/{ontology_id}/versions"))

    async def get_version(self, ontology_id: str, version: str) -> dict[str, Any] | None:
        if not ontology_id or not ontology_id.strip():
            raise ValueError("ontology_id is required")
        if not version or not version.strip():
            raise ValueError("version is required")
        try:
            data = await self._get(f"/v1/ontologies/{ontology_id}/versions/{version}")
        except UpstreamError as exc:
            if exc.status_code == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    async def extraction_profile(self, ontology_id: str, version: str) -> dict[str, Any]:
        if not ontology_id or not ontology_id.strip():
            raise ValueError("ontology_id is required")
        if not version or not version.strip():
            raise ValueError("version is required")
        data = await self._get(
            f"/v1/ontologies/{ontology_id}/versions/{version}/extraction-profile"
        )
        return data if isinstance(data, dict) else {}

    async def list_facts(self, ontology_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] | None = {"ontology_id": ontology_id} if ontology_id else None
        return self._coerce_list(await self._get("/v1/facts", params=params))

    async def list_quarantine(self) -> list[dict[str, Any]]:
        return self._coerce_list(await self._get("/v1/quarantine"))

    async def list_sync(self) -> list[dict[str, Any]]:
        return self._coerce_list(await self._get("/v1/projection-status"))

    async def audit(self) -> list[dict[str, Any]]:
        return self._coerce_list(await self._get("/v1/ontology-audit"))
