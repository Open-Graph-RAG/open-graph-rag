from __future__ import annotations
import json
from pathlib import Path
from typing import Any

class FixtureAdapter:
    """Offline deterministic adapter; explicitly not LightRAG evidence."""
    def __init__(self, fixture_path: Path):
        self.rows = {x["case_id"]: x for x in _jsonl(fixture_path)}
    def answer(self, case: dict[str, Any], corpus: list[dict[str, Any]], arm: str) -> dict[str, Any]:
        row = self.rows.get(case["id"])
        if row is None: raise ValueError(f"missing offline fixture for {case['id']}")
        result = row.get("answers", {}).get(arm, row.get("answer"))
        if not isinstance(result, dict): raise ValueError(f"fixture has no answer for {case['id']} / {arm}")
        return {"answer": result, "adapter": "offline_fixture", "evidence_status": "FIXTURE_ONLY_NOT_LIGHTRAG_EVIDENCE"}

def _jsonl(p: Path):
    with p.open(encoding="utf-8") as f:
        for n,line in enumerate(f,1):
            if line.strip():
                try: yield json.loads(line)
                except json.JSONDecodeError as e: raise ValueError(f"{p}:{n}: {e}") from e
