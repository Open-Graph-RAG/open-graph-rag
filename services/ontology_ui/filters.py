"""Jinja2 filters and global helpers used by the ontology UI templates.

The filters stay small and side-effect free; everything visual is owned
by the templates and the design system in `static/style.css`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def isoformat(value: Any) -> str:
    """Render a timestamp as `YYYY-MM-DD HH:MM:SS UTC`.

    Accepts ISO-8601 strings, `datetime` objects, or `None`. The
    upstream API returns naive UTC timestamps; we treat naive inputs
    as UTC. Returns an em-dash for missing or unparseable values.
    """
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def truncate_hash(value: Any, length: int = 12) -> str:
    """Show a short prefix of a content hash for table rows."""
    if not isinstance(value, str) or not value:
        return "—"
    if length < 4:
        length = 4
    if len(value) <= length:
        return value
    return value[:length]


def pretty_json(value: Any, indent: int = 2) -> str:
    """Render a Python value as a stable, human-readable JSON string."""
    try:
        return json.dumps(value, indent=indent, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def status_kind(value: Any) -> str:
    """Map a fact or version status into a normalized pill class."""
    if not isinstance(value, str):
        return "neutral"
    normalized = value.strip().lower()
    mapping = {
        "published": "published",
        "draft": "draft",
        "accepted": "accepted",
        "quarantined": "quarantined",
        "observed": "observed",
        "delivered": "delivered",
        "failed": "failed",
        "pending": "pending",
        "rejected": "failed",
    }
    return mapping.get(normalized, "neutral")


def fact_kind_label(value: Any) -> str:
    if not isinstance(value, str):
        return "—"
    normalized = value.strip().lower()
    return {"entity": "Entity", "relation": "Relation"}.get(normalized, value)


def register(env: Any) -> None:
    """Register filters and globals on a Jinja2 environment."""
    env.filters["isoformat"] = isoformat
    env.filters["truncate_hash"] = truncate_hash
    env.filters["pretty_json"] = pretty_json
    env.filters["status_kind"] = status_kind
    env.filters["fact_kind_label"] = fact_kind_label
