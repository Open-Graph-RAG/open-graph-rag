#!/usr/bin/env python3
"""Generate secrets once; never overwrite an existing configuration."""
import argparse
from pathlib import Path
import os
import re
import secrets
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".env"
KEY_LINE = re.compile(rb"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=")
SECRET_LENGTHS = {
    "MONGO_PASSWORD": 32, "POSTGRES_PASSWORD": 32,
    "LIGHTRAG_API_KEY": 32, "MCP_TOKEN": 32,
    "CREDS_KEY": 32, "CREDS_IV": 16,
    "JWT_SECRET": 48, "JWT_REFRESH_SECRET": 48,
    "AP_POSTGRES_PASSWORD": 32, "AP_ENCRYPTION_KEY": 16,
    "AP_JWT_SECRET": 32, "HEADROOM_PROXY_TOKEN": 32,
}


def initialize_env(root: Path = ROOT) -> str:
    """Create a new private environment file without replacing an existing one."""
    target = root / ".env"
    content = (root / ".env.example").read_text()
    for key, size in SECRET_LENGTHS.items():
        content = content.replace(f"{key}=GENERATE", f"{key}={secrets.token_hex(size)}")
    for placeholder in ("GENERATE_ONTOLOGY_ADMIN", "GENERATE_ONTOLOGY_READER"):
        content = content.replace(placeholder, secrets.token_hex(32))
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(".env already exists: no changes made.")
    with os.fdopen(fd, "w") as env_file:
        env_file.write(content)
    return "Created .env. Set CHAT_API_KEY and KNOWLEDGE_API_KEY."


def add_headroom_settings(path: Path = DEFAULT_ENV_FILE) -> list[str]:
    """Add only the missing Headroom token, preserving the existing file bytes."""
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlinked .env file.")
    original = path.read_bytes()
    present = {
        match.group(1).decode("ascii")
        for line in original.splitlines()
        if (match := KEY_LINE.match(line)) is not None
    }
    additions = (
        [("HEADROOM_PROXY_TOKEN", secrets.token_hex(32))]
        if "HEADROOM_PROXY_TOKEN" not in present
        else []
    )
    if not additions:
        return []

    separator = b"" if not original or original.endswith((b"\n", b"\r")) else b"\n"
    appended = separator + b"\n# Headroom settings (added by scripts/init_env.py)\n"
    appended += b"".join(f"{key}={value}\n".encode("ascii") for key, value in additions)
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(original)
            temporary.write(appended)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return [key for key, _ in additions]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrate-headroom",
        action="store_true",
        help="add a generated Headroom token to an existing .env without replacing settings",
    )
    args = parser.parse_args(argv)
    if args.migrate_headroom:
        added = add_headroom_settings()
        if added:
            print("Added missing Headroom setting(s) to .env.")
        else:
            print("Headroom settings already present; .env unchanged.")
    else:
        print(initialize_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
