#!/usr/bin/env python3
"""Index useful text files from this repository into the local LightRAG workspace."""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = {".cjs", ".js", ".json", ".md", ".py", ".sh", ".sql", ".toml", ".ts", ".tsx", ".yaml", ".yml"}
IGNORED_DIRS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__",
    "backups", "build", "dist", "env", "node_modules", "sample-data", "venv",
}
IGNORED_NAMES = {"package-lock.json", "pnpm-lock.yaml", "requirements.lock", "yarn.lock"}
MAX_FILE_BYTES = 256_000


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE settings without overriding the current environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def candidates():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(ROOT)
        if any(part in IGNORED_DIRS for part in relative.parts[:-1]):
            continue
        if path.name in IGNORED_NAMES or path.name.startswith(".env"):
            continue
        if path.suffix.lower() not in EXTENSIONS or path.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source = f"codebase--{relative.as_posix().replace('/', '__')}#sha256={digest}"
        yield relative.as_posix(), source, content


def api(method: str, endpoint: str, payload=None):
    url = os.environ["LIGHTRAG_URL"].rstrip("/") + endpoint
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method, headers={
        "X-API-Key": os.environ["LIGHTRAG_API_KEY"],
        "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"LightRAG request {method} {endpoint} failed: {exc}") from exc


def indexed_sources():
    page = 1
    found = {}
    while True:
        result = api("POST", "/documents/paginated", {"page": page, "page_size": 200})
        for doc in result.get("documents", []):
            source = doc.get("file_path", "")
            if "#sha256=" in source:
                found[source] = doc.get("status", "").upper()
        if not result.get("pagination", {}).get("has_next"):
            return found
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list eligible files without contacting LightRAG")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds to wait for each document (default: 1800)")
    args = parser.parse_args()

    files = list(candidates())
    print(f"Eligible files: {len(files)} (UTF-8 source/docs/config; max {MAX_FILE_BYTES} bytes each)")
    if args.dry_run:
        for relative, _, _ in files:
            print(relative)
        return 0

    load_env_file(ROOT / ".env")
    os.environ.setdefault("LIGHTRAG_URL", "http://127.0.0.1:9621")
    if not os.environ.get("LIGHTRAG_API_KEY"):
        parser.error("Set LIGHTRAG_API_KEY in .env or the environment")

    existing = indexed_sources()
    pending = []
    tracks = {}
    unchanged = 0
    for path, source, content in files:
        digest = source.rsplit("=", 1)[1]
        legacy_source = f"{Path(path).name}#sha256={digest}"
        watch_source = source if source in existing else legacy_source if legacy_source in existing else None
        if watch_source is None:
            pending.append((path, source, content))
        elif existing[watch_source] == "PROCESSED":
            unchanged += 1
        else:
            tracks[watch_source] = (path, "")
    print(f"Already indexed unchanged: {unchanged}; to ingest or finish: {len(pending) + len(tracks)}")
    for relative, source, content in pending:
        text = f"Repository file: {relative}\nSource SHA-256: {source.rsplit('=', 1)[1]}\n\n{content}"
        result = api("POST", "/documents/text", {"text": text, "file_source": source})
        if result.get("status") != "success":
            raise RuntimeError(f"LightRAG did not accept {relative}: {result.get('status')}")
        tracks[source] = (relative, result["track_id"])
        print(f"QUEUED {relative}: track={result['track_id']}")

    if tracks:
        deadline = time.monotonic() + args.timeout
        reported = set()
        while time.monotonic() < deadline:
            current = indexed_sources()
            failed = [path for source, (path, _) in tracks.items() if current.get(source) == "FAILED"]
            if failed:
                raise RuntimeError(f"LightRAG ingestion failed: {', '.join(failed)}")
            for source, (path, track) in tracks.items():
                if current.get(source) == "PROCESSED" and source not in reported:
                    print(f"PROCESSED {path}" + (f": track={track}" if track else " (resumed)"))
                    reported.add(source)
            if len(reported) == len(tracks):
                break
            time.sleep(5)
        else:
            raise TimeoutError(f"Timed out waiting for {len(tracks) - len(reported)} documents to process")

    counts = api("GET", "/documents/status_counts")
    print("LightRAG status counts:", json.dumps(counts, sort_keys=True))
    print(f"Ingestion complete. Sources use content hashes, so an unchanged rerun skips all {len(files)} files.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
