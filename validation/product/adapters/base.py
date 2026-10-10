from __future__ import annotations
from typing import Any, Protocol

class Adapter(Protocol):
    def answer(self, case: dict[str, Any], corpus: list[dict[str, Any]], arm: str) -> dict[str, Any]: ...
