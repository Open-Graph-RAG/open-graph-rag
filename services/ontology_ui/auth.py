"""Session helpers for the ontology console.

The UI never accepts a static API key. Users log in with their ontology
bearer token plus the workspace name; both are stored in a Starlette
signed-cookie session. Each request handler constructs a per-request
`OntologyClient` bound to the session's token and workspace.

A `https_only` flag lets the same image run on plain `127.0.0.1` for
local development while forcing secure cookies when deployed behind
HTTPS in production.
"""
from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER


SESSION_KEYS = ("token", "workspace")


class LoginRedirect(Exception):
    """Raised by `get_principal` when the session is missing or invalid.

    The FastAPI app registers a handler that turns this into a 303 to
    `/login?next=<original-path>`. Using an exception (rather than
    returning a `RedirectResponse` from `get_principal`) keeps the
    dependency typed as a plain principal dict.
    """

    def __init__(self, response: RedirectResponse):
        self.response = response
        super().__init__("login required")


def new_session_secret() -> str:
    """Generate a development-only session secret. Production should set
    `ONTOLOGY_UI_SESSION_SECRET` to a long, persistent value."""
    return secrets.token_hex(48)


def is_authenticated(request: Request) -> bool:
    session = request.session
    token = session.get("token")
    workspace = session.get("workspace")
    return (
        isinstance(token, str)
        and len(token) >= 32
        and isinstance(workspace, str)
        and bool(workspace.strip())
    )


def get_principal(request: Request) -> dict[str, str]:
    """Return the authenticated principal or raise `LoginRedirect`.

    The app's exception handler converts `LoginRedirect` into a 303
    to `/login?next=<original-path>`.
    """
    if not is_authenticated(request):
        raise LoginRedirect(_redirect_to_login(request))
    return {
        "token": request.session["token"],
        "workspace": request.session["workspace"],
        "token_handle": _handle_for(request.session["token"]),
    }


def _handle_for(token: str) -> str:
    """Render a non-reversible handle for the top bar, e.g. `••••a3f1`."""
    if not isinstance(token, str) or len(token) < 4:
        return "••••"
    return "••••" + token[-4:]


def _redirect_to_login(request: Request) -> RedirectResponse:
    path = request.url.path
    next_arg = quote(path, safe="/")
    target = f"/login?next={next_arg}"
    return RedirectResponse(url=target, status_code=HTTP_303_SEE_OTHER)


def clear_session(request: Request) -> None:
    """Empty the session so the next read returns no principal."""
    for key in SESSION_KEYS:
        request.session.pop(key, None)


def login(
    request: Request,
    token: str,
    workspace: str,
    *,
    min_token_length: int = 32,
) -> tuple[bool, str | None]:
    """Validate and store credentials in the session.

    Returns `(True, None)` on success. On failure, returns `(False, message)`
    and does not touch the session. The `min_token_length` defaults to the
    minimum required by the ontology service itself.
    """
    if not isinstance(token, str):
        return False, "Token is required."
    token = token.strip()
    if len(token) < min_token_length:
        return False, f"Token must be at least {min_token_length} characters."
    if not isinstance(workspace, str) or not workspace.strip():
        return False, "Workspace is required."
    request.session["token"] = token
    request.session["workspace"] = workspace.strip()
    return True, None
