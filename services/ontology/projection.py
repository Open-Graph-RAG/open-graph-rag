"""Outbox worker helpers for projecting accepted canonical facts."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

from .adapters.lightrag import LightRAGAdapter


class ProjectionOutbox(Protocol):
    def lease_outbox(self, limit: int = 10, workspace: str | None = None) -> list[dict[str, Any]]: ...
    def complete_outbox(self, outbox_id: Any, lease_token: str | None = None) -> None: ...
    def fail_outbox(self, outbox_id: Any, error: str, lease_token: str | None = None) -> None: ...
    def projection_guard(self, row: dict[str, Any]): ...


async def sync_outbox(
    store: ProjectionOutbox,
    adapter: LightRAGAdapter,
    *,
    limit: int = 10,
    workspace: str | None = None,
) -> dict[str, int]:
    """Project one leased batch, recording each item's outcome independently.

    The store owns lease expiry and retry scheduling. A LightRAG/API failure is
    reported through ``fail_outbox`` and does not prevent later items in the
    batch from being attempted.
    """
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    counts = {"leased": 0, "projected": 0, "failed": 0, "skipped": 0}
    if not workspace:
        raise ValueError("a LightRAG workspace is required for projection")
    rows = store.lease_outbox(limit=limit, workspace=workspace)
    counts["leased"] = len(rows)
    for row in rows:
        outbox_id = row["id"]
        lease_token = row.get("lease_token")
        with store.projection_guard(row) as current:
            if not current:
                counts["skipped"] += 1
                continue
            try:
                fact = row["fact"]
                if not isinstance(fact, dict):
                    raise ValueError("outbox fact must be an object")
                if str(row.get("workspace", "")) != workspace:
                    raise ValueError("outbox workspace does not match configured LightRAG workspace")
                # A workspace mismatch would defeat projection ID isolation.
                if str(row.get("workspace", fact.get("workspace", ""))) != str(fact.get("workspace", "")):
                    raise ValueError("outbox workspace does not match canonical fact workspace")
                await adapter.project(fact)
                store.complete_outbox(outbox_id, lease_token)
                counts["projected"] += 1
            except Exception as exc:
                store.fail_outbox(outbox_id, f"{type(exc).__name__}: {exc}", lease_token)
                counts["failed"] += 1
    return counts


async def run_worker() -> None:
    """Run the PostgreSQL outbox projector until the process is stopped."""
    from .store import Store

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    store = Store(os.environ["ONTOLOGY_DATABASE_URL"])
    store.initialize()
    workspace = os.environ["LIGHTRAG_WORKSPACE"]
    adapter = LightRAGAdapter(os.environ["LIGHTRAG_URL"], os.environ["LIGHTRAG_API_KEY"])
    idle_seconds = float(os.environ.get("PROJECTION_IDLE_SECONDS", "2"))
    try:
        while True:
            try:
                result = await sync_outbox(store, adapter, limit=10, workspace=workspace)
                if result["leased"] == 0:
                    await asyncio.sleep(idle_seconds)
            except Exception:
                logging.exception("Ontology projection batch failed")
                await asyncio.sleep(idle_seconds)
    finally:
        await adapter.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
