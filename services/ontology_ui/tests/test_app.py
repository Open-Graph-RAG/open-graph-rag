"""Unit tests for the ontology UI server.

The tests patch the `OntologyClient` methods on the module that owns
them (`services.ontology_ui.app`) so every route handler resolves to
the same in-memory fixture. The real ontology service and the real
network are never touched.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from services.ontology_ui import app as app_module  # noqa: E402
from services.ontology_ui.app import create_app  # noqa: E402
from services.ontology_ui.auth import SessionStore  # noqa: E402


SESSION_SECRET = "x" * 64
VALID_TOKEN = "t" * 40
VALID_WORKSPACE = "company_governed"


class FakeUpstream:
    """Stub for `OntologyClient` whose methods read from in-memory fixtures."""

    def __init__(self, fixtures: dict[str, Any]):
        self.fixtures = fixtures
        self.closed = False
        self.last_token: str | None = None
        self.last_workspace: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True

    async def health(self) -> dict[str, Any]:
        return self.fixtures.get("health", {"status": "ok"})

    async def list_ontologies(self) -> list[dict[str, Any]]:
        return list(self.fixtures.get("ontologies", []))

    async def list_versions(self, ontology_id: str) -> list[dict[str, Any]]:
        versions = self.fixtures.get("versions", [])
        return [v for v in versions if v.get("ontology_id") == ontology_id]

    async def get_version(self, ontology_id: str, version: str) -> dict[str, Any] | None:
        for row in self.fixtures.get("versions", []):
            if row.get("ontology_id") == ontology_id and row.get("version") == version:
                return row.get("definition", {"id": ontology_id, "version": version})
        return None

    async def extraction_profile(self, ontology_id: str, version: str) -> dict[str, Any]:
        return {
            "ontology_id": ontology_id,
            "ontology_version": version,
            "profile": "Profile text",
            "governance": "guidance_only",
        }

    async def list_facts(self, ontology_id: str | None = None) -> list[dict[str, Any]]:
        facts = self.fixtures.get("facts", [])
        if ontology_id:
            return [f for f in facts if f.get("ontology_id") == ontology_id]
        return list(facts)

    async def list_quarantine(self) -> list[dict[str, Any]]:
        return list(self.fixtures.get("quarantine", []))

    async def list_sync(self) -> list[dict[str, Any]]:
        return list(self.fixtures.get("sync_rows", []))

    async def audit(self) -> list[dict[str, Any]]:
        return list(self.fixtures.get("audit_rows", []))


def _make_client(
    fixtures: dict[str, Any],
    *,
    raise_on: dict[str, BaseException] | None = None,
    status_overrides: dict[str, int] | None = None,
    https_only: bool = False,
) -> TestClient:
    """Build a TestClient whose OntologyClient constructor returns a FakeUpstream.

    The `raise_on` map can make a named method raise an exception. The
    `status_overrides` map can force a method to raise an UpstreamError
    with a given status code (mimicking a non-2xx upstream response).
    """
    raise_on = raise_on or {}
    status_overrides = status_overrides or {}

    class _Factory:
        def __init__(self, fixtures: dict[str, Any]):
            self.fixtures = fixtures

        def __call__(self, api_url: str, token: str, workspace: str, *, client=None) -> FakeUpstream:
            fake = FakeUpstream(self.fixtures)
            fake.last_token = token
            fake.last_workspace = workspace
            return fake

    factory = _Factory(fixtures)

    # Wrap the factory to honour raise_on / status_overrides.
    def _build(base_url: str, token: str, workspace: str, *, client=None) -> Any:
        fake = factory(base_url, token, workspace, client=client)
        from services.ontology_ui.api_client import UpstreamError
        for name, exc in raise_on.items():
            async def _raising(*args, _exc=exc, **kwargs):
                if isinstance(_exc, BaseException) and _exc.__class__.__name__ == "ConnectError":
                    raise UpstreamError(502, f"Upstream unavailable: {_exc}") from _exc
                raise _exc
            setattr(fake, name, _raising)
        for name, status in status_overrides.items():
            async def _fail(*args, _status=status, **kwargs):
                raise UpstreamError(_status, f"forced {_status}")
            setattr(fake, name, _fail)
        return fake

    app = create_app(
        api_url="http://upstream.test",
        session_secret=SESSION_SECRET,
        https_only=https_only,
    )
    # Patch the OntologyClient used by the app to return our fake.
    patches = [
        patch.object(app_module, "OntologyClient", side_effect=_build),
    ]
    for p in patches:
        p.start()
    client = TestClient(app, follow_redirects=False,
                        base_url="https://testserver" if https_only else "http://testserver")
    client._patches = patches  # type: ignore[attr-defined]
    return client


def _teardown(client: TestClient) -> None:
    for p in getattr(client, "_patches", []):
        p.stop()


def _login(
    client: TestClient,
    token: str = VALID_TOKEN,
    workspace: str = VALID_WORKSPACE,
) -> None:
    response = client.post(
        "/login",
        data={"token": token, "workspace": workspace, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text


def _default_fixtures() -> dict[str, Any]:
    return {
        "ontologies": [
            {"id": "ogr-core", "active_version": "1.0.0"},
            {"id": "empty", "active_version": None},
        ],
        "versions": [
            {
                "ontology_id": "ogr-core",
                "version": "1.0.0",
                "status": "published",
                "content_hash": "abc123def456",
                "created_by": "admin",
                "created_at": "2025-01-01T00:00:00Z",
                "published_by": "admin",
                "published_at": "2025-01-02T00:00:00Z",
                "definition": {
                    "id": "ogr-core",
                    "version": "1.0.0",
                    "status": "published",
                    "entities": {
                        "System": {
                            "description": "Software or infrastructure system",
                            "properties": {
                                "name": {"type": "string", "required": True},
                                "lifecycle": {"type": "string", "enum": ["planned", "active"]},
                            },
                        }
                    },
                    "relations": {
                        "DEPENDS_ON": {
                            "source": ["System"],
                            "target": ["System"],
                            "directed": True,
                            "description": "System depends on another system",
                        }
                    },
                },
            }
        ],
        "facts": [
            {
                "id": "f-1",
                "kind": "entity",
                "ontology_id": "ogr-core",
                "ontology_version": "1.0.0",
                "workspace": VALID_WORKSPACE,
                "entity_type": "System",
                "properties": {"name": "LibreChat"},
            },
            {
                "id": "f-2",
                "kind": "relation",
                "ontology_id": "ogr-core",
                "ontology_version": "1.0.0",
                "workspace": VALID_WORKSPACE,
                "predicate": "DEPENDS_ON",
                "subject_id": "f-1",
                "object_id": "f-1",
                "directed": True,
            },
        ],
        "sync_rows": [
            {"id": 1, "fact_id": "f-1", "delivered_at": "2025-01-01T00:00:00Z", "attempts": 1},
            {"id": 2, "fact_id": "f-2", "last_error": "boom", "attempts": 3},
            {"id": 3, "fact_id": "f-3", "attempts": 0},
        ],
        "audit_rows": [
            {"sequence": 1, "actor": "admin", "action": "ontology.published", "subject": "ogr-core@1.0.0", "created_at": "2025-01-01T00:00:00Z"},
        ],
        "quarantine": [],
    }


class HealthTests(unittest.TestCase):
    def test_health_returns_200_when_upstream_healthy(self) -> None:
        client = _make_client(_default_fixtures())
        try:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")
        finally:
            _teardown(client)


class SessionStoreCapacityTests(unittest.TestCase):
    def test_one_principal_cannot_fill_global_session_capacity(self):
        store=SessionStore(ttl=3600,maximum=3,maximum_per_principal=2)
        old=store.create("valid-reader-token","workspace")
        current=store.create("valid-reader-token","workspace")
        latest=store.create("valid-reader-token","workspace")
        other=store.create("other-reader-token","workspace")
        self.assertIsNone(store.get(old))
        self.assertIsNotNone(store.get(current))
        self.assertIsNotNone(store.get(latest))
        self.assertIsNotNone(store.get(other))
        with self.assertRaisesRegex(RuntimeError,"capacity"):
            store.create("third-principal-token","workspace")


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _make_client(_default_fixtures())

    def tearDown(self) -> None:
        _teardown(self.client)

    def test_root_redirects_to_login_without_session(self) -> None:
        response = self.client.get("/", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertIn("/login", response.headers["location"])

    def test_login_get_renders_form(self) -> None:
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("name=\"token\"", response.text)
        self.assertIn("name=\"workspace\"", response.text)

    def test_login_post_with_valid_form_redirects(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "/facts"},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(response.headers["location"], "/facts")
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn(VALID_TOKEN, cookie)

    def test_capacity_failure_preserves_existing_session(self) -> None:
        _login(self.client)
        sid = self.client.cookies.get("ontology_session")
        store = self.client.app.state.session_store
        store.maximum = 1
        response = self.client.post("/login", data={
            "token": "u" * 40, "workspace": VALID_WORKSPACE, "next": "/"},
            follow_redirects=False)
        self.assertEqual(response.status_code, 503)
        self.assertIsNotNone(store.get(sid))
        self.assertEqual(self.client.get("/facts").status_code, 200)

    def test_secure_cookie_legacy_cookie_expiry_and_relogin_revocation(self) -> None:
        client = _make_client(_default_fixtures(), https_only=True)
        try:
            _login(client)
            first_sid = client.cookies.get("ontology_session")
            self.assertTrue(first_sid)
            self.assertTrue(client.app.state.session_store.get(first_sid))
            response = client.get("/login", headers={"Cookie": "session=legacy-signed-value"})
            self.assertIn("session=\"\"", response.headers.get("set-cookie", ""))
            self.assertIn("Secure", response.headers.get("set-cookie", ""))
            response = client.post("/login", data={"token": VALID_TOKEN,
                "workspace": VALID_WORKSPACE, "next": "/"}, follow_redirects=False)
            second_sid = client.cookies.get("ontology_session")
            self.assertNotEqual(first_sid, second_sid)
            self.assertIsNone(client.app.state.session_store.get(first_sid))
            self.assertTrue(client.app.state.session_store.get(second_sid))
            self.assertIn("Secure", response.headers.get("set-cookie", ""))
        finally:
            _teardown(client)

    def test_expired_and_revoked_sessions_are_rejected(self) -> None:
        import time
        _login(self.client)
        sid_cookie = self.client.cookies.get("ontology_session")
        self.assertTrue(sid_cookie)
        store = self.client.app.state.session_store
        with store._lock:
            token, workspace, _ = store._sessions[sid_cookie]
            store._sessions[sid_cookie] = (token, workspace, time.monotonic() - 1)
        response = self.client.get("/facts", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertIn('ontology_session=""', response.headers.get("set-cookie", ""))
        store.ttl = 3600
        _login(self.client)
        sid = self.client.cookies.get("ontology_session")
        store.revoke(sid)
        response = self.client.get("/facts", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertIn('ontology_session=""', response.headers.get("set-cookie", ""))

    def test_login_post_with_same_origin_origin_header_succeeds(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "/facts"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/facts")
        self.assertIn("ontology_session=", response.headers.get("set-cookie", ""))

    def test_login_post_rejects_cross_origin_without_setting_cookie(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "/facts"},
            headers={"Origin": "https://attacker.example"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)

    def test_login_post_rejects_cross_origin_referer_fallback(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "/facts"},
            headers={"Referer": "https://attacker.example/login"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)

    def test_login_post_with_too_short_token_rejected(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": "short", "workspace": VALID_WORKSPACE, "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Token must be at least", response.text)

    def test_login_post_with_missing_workspace_rejected(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": "  ", "next": "/"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Workspace", response.text)

    def test_workspace_denial_does_not_create_browser_session(self) -> None:
        client = _make_client(_default_fixtures(), status_overrides={"list_ontologies": 403})
        try:
            response = client.post("/login", data={"token": VALID_TOKEN,
                "workspace": "other-workspace", "next": "/"}, follow_redirects=False)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("ontology_session", response.headers.get("set-cookie", ""))
        finally:
            _teardown(client)

    def test_upstream_auth_denial_preserves_auth_status(self) -> None:
        client = _make_client(_default_fixtures(), status_overrides={"list_ontologies": 401})
        try:
            response = client.post("/login", data={"token": VALID_TOKEN,
                "workspace": VALID_WORKSPACE, "next": "/"}, follow_redirects=False)
            self.assertEqual(response.status_code, 401)
            self.assertIn("access denied", response.text.lower())
        finally:
            _teardown(client)

    def test_upstream_outages_are_not_reported_as_invalid_credentials(self) -> None:
        for upstream_status in (500, 502):
            with self.subTest(upstream_status=upstream_status):
                client = _make_client(_default_fixtures(),
                    status_overrides={"list_ontologies": upstream_status})
                try:
                    response = client.post("/login", data={"token": VALID_TOKEN,
                        "workspace": VALID_WORKSPACE, "next": "/"}, follow_redirects=False)
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("temporarily unavailable", response.text.lower())
                    self.assertNotIn("access denied", response.text.lower())
                finally:
                    _teardown(client)

    def test_logout_clears_session(self) -> None:
        _login(self.client)
        response = self.client.post("/logout", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        response = self.client.get("/", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        self.assertIn("/login", response.headers["location"])

    def test_logout_rejects_cross_origin_and_preserves_session(self) -> None:
        _login(self.client)
        response = self.client.post(
            "/logout",
            headers={"Origin": "https://attacker.example"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)
        dashboard = self.client.get("/", follow_redirects=False)
        self.assertEqual(dashboard.status_code, 200)


class FactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = _default_fixtures()
        self.client = _make_client(self.fixtures)
        _login(self.client)

    def tearDown(self) -> None:
        _teardown(self.client)

    def test_facts_page_renders(self) -> None:
        response = self.client.get("/facts")
        self.assertEqual(response.status_code, 200)
        self.assertIn("f-1", response.text)
        self.assertIn("f-2", response.text)
        self.assertIn("DEPENDS_ON", response.text)

    def test_facts_filter_by_kind(self) -> None:
        response = self.client.get("/facts?kind=relation")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("LibreChat", response.text)
        self.assertIn("DEPENDS_ON", response.text)

    def test_facts_filter_by_predicate(self) -> None:
        response = self.client.get("/facts?predicate=DEPENDS")
        self.assertEqual(response.status_code, 200)
        self.assertIn("DEPENDS_ON", response.text)
        self.assertNotIn("LibreChat", response.text)

    def test_facts_session_token_forwarded_to_upstream(self) -> None:
        # Verify the route forwards the session token to OntologyClient by
        # patching the factory and capturing the call arguments.
        from unittest.mock import patch
        from services.ontology_ui import app as app_module
        captured: dict[str, Any] = {}

        def _capture_build(base_url, token, workspace, *, client=None):
            captured["base_url"] = base_url
            captured["token"] = token
            captured["workspace"] = workspace
            return FakeUpstream(self.fixtures)

        with patch.object(app_module, "OntologyClient", side_effect=_capture_build):
            response = self.client.get("/facts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["token"], VALID_TOKEN)
        self.assertEqual(captured["workspace"], VALID_WORKSPACE)

    def test_facts_pagination_caps_page_size(self) -> None:
        response = self.client.get("/facts?page_size=5000")
        self.assertEqual(response.status_code, 200)
        self.assertIn("2 facts", response.text)


class UpstreamErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = _default_fixtures()
        self.client = _make_client(self.fixtures)
        _login(self.client)

    def tearDown(self) -> None:
        _teardown(self.client)

    def test_401_clears_session_and_redirects_to_login(self) -> None:
        client = _make_client(_default_fixtures(), status_overrides={"list_facts": 401})
        try:
            _login(client)
            response = client.get("/facts", follow_redirects=False)
            self.assertIn(response.status_code, (302, 303))
            self.assertIn("/login", response.headers["location"])
            self.assertIn("next=", response.headers["location"])
        finally:
            _teardown(client)

    def test_403_renders_error_panel_without_clearing_session(self) -> None:
        client = _make_client(_default_fixtures(), status_overrides={"list_facts": 403})
        try:
            _login(client)
            response = client.get("/facts")
            self.assertEqual(response.status_code, 403)
            self.assertIn("forced 403", response.text)
        finally:
            _teardown(client)

    def test_502_renders_unavailable_panel(self) -> None:
        import httpx
        client = _make_client(
            _default_fixtures(),
            raise_on={"list_facts": httpx.ConnectError("upstream down")},
        )
        try:
            _login(client)
            response = client.get("/facts")
            self.assertEqual(response.status_code, 502)
            self.assertIn("Upstream unavailable", response.text)
        finally:
            _teardown(client)

    def test_404_on_ontology_version_renders_not_found(self) -> None:
        client = _make_client(_default_fixtures(), status_overrides={"get_version": 404})
        try:
            _login(client)
            response = client.get("/ontologies/ogr-core/versions/9.9.9")
            self.assertEqual(response.status_code, 404)
            self.assertIn("not found", response.text.lower())
        finally:
            _teardown(client)


class DashboardTests(unittest.TestCase):
    def test_dashboard_renders_kpis(self) -> None:
        client = _make_client(_default_fixtures())
        try:
            _login(client)
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Ontologies", response.text)
            self.assertIn("Facts", response.text)
            self.assertIn("Outbox", response.text)
        finally:
            _teardown(client)


class ReviewFixTests(unittest.TestCase):
    """Regression tests for the post-review fixes (C1/R1/R2/R3/R4/R6/R7/O1)."""

    def setUp(self) -> None:
        self.fixtures = _default_fixtures()
        self.client = _make_client(self.fixtures)
        _login(self.client)

    def tearDown(self) -> None:
        _teardown(self.client)

    # --- C1: /facts?q must not crash ---
    def test_facts_free_text_search_does_not_crash(self) -> None:
        response = self.client.get("/facts?q=LibreChat")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LibreChat", response.text)

    def test_facts_free_text_search_narrows_results(self) -> None:
        response = self.client.get("/facts?q=DEPENDS")
        self.assertEqual(response.status_code, 200)
        self.assertIn("f-2", response.text)
        self.assertNotIn("LibreChat", response.text)

    # --- R1: open redirect via `next` is blocked ---
    def test_login_post_blocks_protocol_relative_next(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "//evil.com"},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(response.headers["location"], "/")

    def test_login_post_blocks_absolute_url_next(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "https://evil.com"},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(response.headers["location"], "/")

    def test_login_post_blocks_backslash_next(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "/\\evil.com"},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(response.headers["location"], "/")

    def test_login_post_accepts_safe_relative_next(self) -> None:
        response = self.client.post(
            "/login",
            data={"token": VALID_TOKEN, "workspace": VALID_WORKSPACE, "next": "/facts?page=2"},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        self.assertEqual(response.headers["location"], "/facts?page=2")

    # --- R2: dashboard reuses the shared httpx client ---
    def test_dashboard_passes_shared_client_to_ontology_client(self) -> None:
        captured: dict[str, Any] = {}

        def _capture(base_url, token, workspace, *, client=None):
            captured["base_url"] = base_url
            captured["token"] = token
            captured["workspace"] = workspace
            captured["client"] = client
            return FakeUpstream(self.fixtures)

        with patch.object(app_module, "OntologyClient", side_effect=_capture):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(captured.get("client"), "dashboard must pass the shared httpx client")

    # --- R3: ontology_id is filtered client-side (upstream ignores the param) ---
    def test_facts_ontology_id_filter_applies_even_when_upstream_ignores_it(self) -> None:
        # The real upstream `services/ontology/api.py:139-141` discards the
        # `ontology_id` query param. The UI route must still apply the
        # filter on the client so the page does not show facts from
        # other ontologies. This test pins the regression: a foreign-
        # ontology fact returned by the upstream must not appear in the
        # rendered table when the user filters by another ontology.
        fixtures = _default_fixtures()
        fixtures["facts"].append({
            "id": "f-other",
            "kind": "entity",
            "ontology_id": "other-onto",
            "ontology_version": "9.9.9",
            "workspace": VALID_WORKSPACE,
            "entity_type": "System",
            "properties": {"name": "ForeignTool"},
        })

        async def _ignore_ontology_id(self_unused, ontology_id=None):
            # Stub for FakeUpstream.list_facts: ignore the ontology_id
            # argument and return every fact, mirroring the real upstream.
            return list(fixtures["facts"])

        # Build a fresh client backed by the augmented fixtures, then
        # patch FakeUpstream.list_facts at the *class* level so the
        # instance built by `_make_client` resolves to our stub.
        client = _make_client(fixtures)
        try:
            _login(client)
            with patch.object(FakeUpstream, "list_facts", new=_ignore_ontology_id):
                response = client.get("/facts?ontology_id=ogr-core")
            self.assertEqual(response.status_code, 200)
            # The ogr-core fact must be present; the foreign-ontology
            # fact must be excluded by the UI's client-side filter.
            self.assertIn("f-1", response.text)
            self.assertNotIn("f-other", response.text)
        finally:
            _teardown(client)

    # --- R4: 401 sets a flash message visible after redirect ---
    def test_401_sets_flash_visible_on_login_page(self) -> None:
        client = _make_client(_default_fixtures(), status_overrides={"list_facts": 401})
        try:
            _login(client)
            response = client.get("/facts", follow_redirects=False)
            self.assertIn(response.status_code, (302, 303))
            location = response.headers["location"]
            # The generic expiry notice is selected by the redirect query.
            login_page = client.get(location, follow_redirects=False)
            self.assertEqual(login_page.status_code, 200)
            self.assertIn("Session expired or token invalid", login_page.text)
        finally:
            _teardown(client)

    # --- R6: pagination URLs URL-encode filter values ---
    def test_pagination_url_encodes_special_characters(self) -> None:
        from services.ontology_ui.app import _pagination_url
        # Special chars (`&`, `=`, `#`) must be percent-encoded so the
        # value round-trips as a single query param, not split into two.
        self.assertEqual(
            _pagination_url("/facts", {"q": "a&b"}, 2),
            "/facts?q=a%26b&page=2",
        )
        self.assertEqual(
            _pagination_url("/facts", {"q": "a=b"}, 2),
            "/facts?q=a%3Db&page=2",
        )
        self.assertEqual(
            _pagination_url("/facts", {"q": "a#b"}, 2),
            "/facts?q=a%23b&page=2",
        )

    # --- R7: htmx requests get a fragment, not the full page ---
    def test_facts_returns_fragment_for_htmx_request(self) -> None:
        response = self.client.get(
            "/facts?kind=relation",
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<!DOCTYPE", response.text)
        # The fragment must still include the filtered rows.
        self.assertIn("f-2", response.text)

    # --- O1: a 200 with a non-list payload from a list endpoint surfaces as 502 ---
    def test_list_facts_502_on_malformed_payload(self) -> None:
        from services.ontology_ui.api_client import OntologyClient, UpstreamError
        import httpx

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b'"upstream returned a JSON string, not a list"')

        transport = httpx.MockTransport(_handler)
        client = OntologyClient(
            base_url="http://upstream.test",
            token=VALID_TOKEN,
            workspace=VALID_WORKSPACE,
            client=httpx.AsyncClient(transport=transport),
        )
        with self.assertRaises(UpstreamError) as cm:
            import asyncio
            asyncio.run(client.list_facts())
        self.assertEqual(cm.exception.status_code, 502)
        self.assertIn("non-list", cm.exception.detail)


class OntologyVersionTests(unittest.TestCase):
    def test_version_renders_entities_and_relations(self) -> None:
        client = _make_client(_default_fixtures())
        try:
            _login(client)
            # Default tab is entities; verify the entity name and a property
            # are rendered.
            response = client.get("/ontologies/ogr-core/versions/1.0.0")
            self.assertEqual(response.status_code, 200)
            self.assertIn("System", response.text)
            self.assertIn("name", response.text)
            # Relations tab renders the DEPENDS_ON predicate.
            relations_response = client.get(
                "/ontologies/ogr-core/versions/1.0.0?tab=relations"
            )
            self.assertEqual(relations_response.status_code, 200)
            self.assertIn("DEPENDS_ON", relations_response.text)
        finally:
            _teardown(client)


class ExtractionProfileTests(unittest.TestCase):
    def test_extraction_profile_renders(self) -> None:
        client = _make_client(_default_fixtures())
        try:
            _login(client)
            response = client.get("/ontologies/ogr-core/versions/1.0.0/extraction-profile")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Profile text", response.text)
        finally:
            _teardown(client)


class ListsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = _default_fixtures()
        self.client = _make_client(self.fixtures)
        _login(self.client)

    def tearDown(self) -> None:
        _teardown(self.client)

    def test_ontologies_lists(self) -> None:
        response = self.client.get("/ontologies")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ogr-core", response.text)
        self.assertIn("empty", response.text)

    def test_versions_lists(self) -> None:
        response = self.client.get("/ontologies/ogr-core/versions")
        self.assertEqual(response.status_code, 200)
        self.assertIn("1.0.0", response.text)
        self.assertIn("abc123def456", response.text)

    def test_quarantine_lists(self) -> None:
        response = self.client.get("/quarantine")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No quarantined", response.text)

    def test_projection_lists(self) -> None:
        response = self.client.get("/projection-status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("f-1", response.text)
        self.assertIn("boom", response.text)

    def test_audit_lists(self) -> None:
        response = self.client.get("/audit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ontology.published", response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
