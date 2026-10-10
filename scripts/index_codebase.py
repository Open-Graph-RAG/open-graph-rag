#!/usr/bin/env python3
"""Index useful text files from this repository into the local LightRAG workspace."""

import argparse
import hashlib
import json
import os
import re
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
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r'''(?ix)(?P<prefix>(?<!\w)(?:[\w-]+[_-])*(?:api[_-]?key|access[_-]?token|'''
    r'''client[_-]?secret|password|passwd|secret|token|private[_-]?key|'''
    r'''encryption[_-]?key|signing[_-]?key|authorization|credentials?)\s*[:=]\s*)'''
    r'''(?P<value>"[^"\n]*"|'[^'\n]*'|[^\s,#;}\]]+)'''
)
KNOWN_TOKEN_PATTERN = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"sk-(?:proj|svcacct|ant-api03)-[A-Za-z0-9_-]{20,}|"
    r"AIza[0-9A-Za-z_-]{30,})\b"
)
URL_CREDENTIALS_PATTERN = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@", re.I)


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


def sanitize(content: str) -> str:
    """Redact credential values and common token formats before indexing."""
    content = PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", content)

    def redact_assignment(match):
        value = match.group("value")
        unquoted = value.strip("\"'")
        if unquoted.lower().startswith(("os.environ", "process.env", "${", "$env:")):
            return match.group(0)
        quote = value[0] if value.startswith(("\"", "'")) else ""
        return match.group("prefix") + quote + "[REDACTED]" + quote

    content = SENSITIVE_ASSIGNMENT_PATTERN.sub(redact_assignment, content)
    content = URL_CREDENTIALS_PATTERN.sub(r"\g<scheme>[REDACTED]@", content)
    return KNOWN_TOKEN_PATTERN.sub("[REDACTED TOKEN]", content)


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
            original = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        original_digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        content = sanitize(original)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source = f"codebase--{relative.as_posix().replace('/', '__')}#sha256={digest}"
        yield relative.as_posix(), source, content, original_digest, content != original


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
                found[source] = {
                    "status": doc.get("status", "").upper(),
                    "id": doc.get("id", ""),
                }
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
    print(f"Files with credential-like content to redact: {sum(redacted for *_, redacted in files)}")
    if args.dry_run:
        for relative, _, _, _, _ in files:
            print(relative)
        return 0

    load_env_file(ROOT / ".env")
    os.environ.setdefault("LIGHTRAG_URL", "http://127.0.0.1:9621")
    if not os.environ.get("LIGHTRAG_API_KEY"):
        parser.error("Set LIGHTRAG_API_KEY in .env or the environment")

    existing = indexed_sources()
    removed = 0
    for relative, source, _, original_digest, redacted in files:
        stale_sources = set()
        if redacted:
            stale_sources.add(f"{Path(relative).name}#sha256={original_digest}")
        for old_source in stale_sources:
            old_doc = existing.get(old_source)
            if not old_doc:
                continue
            result = api("DELETE", "/documents/delete_document", {
                "doc_ids": [old_doc["id"]], "delete_file": False, "delete_llm_cache": True,
            })
            if result.get("status") != "deletion_started":
                raise RuntimeError(f"Could not remove stale indexed source for {relative}")
            deadline = time.monotonic() + args.timeout
            while old_source in indexed_sources():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out removing stale source for {relative}")
                time.sleep(2)
            existing.pop(old_source, None)
            removed += 1
            print(f"REMOVED stale indexed source: {relative}")

    pending = []
    tracks = {}
    unchanged = 0
    for path, source, content, original_digest, _ in files:
        digest = source.rsplit("=", 1)[1]
        legacy_sources = [f"{Path(path).name}#sha256={digest}"]
        if original_digest != digest:
            legacy_sources.append(f"{Path(path).name}#sha256={original_digest}")
        watch_source = source if source in existing else next(
            (legacy for legacy in legacy_sources if legacy in existing), None
        )
        if watch_source is None:
            pending.append((path, source, content))
        elif existing[watch_source]["status"] == "PROCESSED":
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
            failed = [
                path for source, (path, _) in tracks.items()
                if current.get(source, {}).get("status") == "FAILED"
            ]
            if failed:
                raise RuntimeError(f"LightRAG ingestion failed: {', '.join(failed)}")
            for source, (path, track) in tracks.items():
                if current.get(source, {}).get("status") == "PROCESSED" and source not in reported:
                    print(f"PROCESSED {path}" + (f": track={track}" if track else " (resumed)"))
                    reported.add(source)
            if len(reported) == len(tracks):
                break
            time.sleep(5)
        else:
            raise TimeoutError(f"Timed out waiting for {len(tracks) - len(reported)} documents to process")

    counts = api("GET", "/documents/status_counts")
    print("LightRAG status counts:", json.dumps(counts, sort_keys=True))
    print(f"Removed unsanitized sources: {removed}")
    print(f"Ingestion complete. Credential-like values are redacted; unchanged reruns skip indexed files.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
