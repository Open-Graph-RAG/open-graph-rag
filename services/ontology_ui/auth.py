"""Opaque, process-local browser sessions for the ontology console."""
from __future__ import annotations

import secrets
import threading
import time
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

COOKIE_NAME = "ontology_session"
SESSION_TTL = 8 * 60 * 60
MAX_SESSIONS = 10_000


class SessionStore:
    """Bounded single-process store. Deploy exactly one UI worker/replica."""

    def __init__(self, ttl: int = SESSION_TTL, maximum: int = MAX_SESSIONS):
        self.ttl, self.maximum = ttl, maximum
        self._sessions: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.RLock()

    def create(self, token: str, workspace: str) -> str:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            if len(self._sessions) >= self.maximum:
                # Expired entries were purged; refuse rather than evicting a live login.
                raise RuntimeError("Ontology UI session capacity reached")
            sid = secrets.token_urlsafe(32)
            self._sessions[sid] = (token, workspace, now + self.ttl)
            return sid

    def get(self, sid: str) -> tuple[str, str] | None:
        with self._lock:
            item = self._sessions.get(sid)
            if item is None:
                return None
            if item[2] <= time.monotonic():
                self._sessions.pop(sid, None)
                return None
            return item[0], item[1]

    def revoke(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def _purge(self, now: float) -> None:
        for sid, (_, _, expires) in list(self._sessions.items()):
            if expires <= now:
                self._sessions.pop(sid, None)


class LoginRedirect(Exception):
    def __init__(self, response: RedirectResponse):
        self.response = response
        super().__init__("login required")


def new_session_secret() -> str:
    """Compatibility helper; secret is no longer used for client-side sessions."""
    return secrets.token_hex(48)


def _sid(request: Request) -> str:
    return request.cookies.get(COOKIE_NAME, "")


def principal(request: Request) -> dict[str, str] | None:
    credentials = request.app.state.session_store.get(_sid(request))
    if not credentials:
        return None
    token, workspace = credentials
    return {"token": token, "workspace": workspace, "token_handle": _handle_for(token)}


def is_authenticated(request: Request) -> bool:
    return principal(request) is not None


def get_principal(request: Request) -> dict[str, str]:
    value = principal(request)
    if value is None:
        raise LoginRedirect(_redirect_to_login(request))
    return value


def _handle_for(token: str) -> str:
    return "••••" + token[-4:] if isinstance(token, str) and len(token) >= 4 else "••••"


def _redirect_to_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={quote(request.url.path, safe='/')}", status_code=HTTP_303_SEE_OTHER)


def issue_session(request: Request, response: Response, token: str, workspace: str) -> None:
    # Re-authentication must invalidate the prior browser credential first.
    request.app.state.session_store.revoke(_sid(request))
    sid = request.app.state.session_store.create(token, workspace)
    response.set_cookie(COOKIE_NAME, sid, max_age=request.app.state.session_store.ttl,
                        httponly=True, secure=request.app.state.https_only,
                        samesite="strict", path="/")


def clear_session(request: Request, response: Response | None = None) -> None:
    request.app.state.session_store.revoke(_sid(request))
    if response is not None:
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True,
                               secure=request.app.state.https_only, samesite="strict")


def login(token: str, workspace: str, *, min_token_length: int = 32) -> tuple[bool, str | None]:
    if not isinstance(token, str) or len(token.strip()) < min_token_length:
        return False, f"Token must be at least {min_token_length} characters."
    if not isinstance(workspace, str) or not workspace.strip():
        return False, "Workspace is required."
    return True, None
