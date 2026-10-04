#!/usr/bin/env python3
"""Append missing Activepieces settings without rewriting an existing .env."""
from pathlib import Path
import os
import re
import secrets
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".env"
VARIABLES = {
    "ACTIVEPIECES_PORT": lambda: "8080",
    "AP_FRONTEND_URL": lambda: "http://localhost:8080",
    "AP_EXECUTION_MODE": lambda: "SANDBOX_CODE_ONLY",
    "AP_WEBHOOK_TIMEOUT_SECONDS": lambda: "30",
    "AP_WORKER_CONCURRENCY": lambda: "1",
    "AP_POSTGRES_DATABASE": lambda: "activepieces",
    "AP_POSTGRES_USERNAME": lambda: "activepieces",
    "AP_POSTGRES_PASSWORD": lambda: secrets.token_hex(32),
    "AP_ENCRYPTION_KEY": lambda: secrets.token_hex(16),
    "AP_JWT_SECRET": lambda: secrets.token_hex(32),
}
KEY_LINE = re.compile(rb"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=")


def add_missing_settings(path: Path = DEFAULT_ENV_FILE) -> list[str]:
    """Append absent keys while preserving every existing byte and the mode."""
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlinked .env file.")
    original = path.read_bytes()
    present = {
        match.group(1).decode("ascii")
        for line in original.splitlines()
        if (match := KEY_LINE.match(line)) is not None
    }
    additions = [(key, factory()) for key, factory in VARIABLES.items() if key not in present]
    if not additions:
        return []

    separator = b"" if not original or original.endswith((b"\n", b"\r")) else b"\n"
    appended = separator + b"\n# Activepieces settings (added by scripts/add_activepieces_env.py)\n"
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


if __name__ == "__main__":
    env_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ENV_FILE
    added = add_missing_settings(env_path)
    if added:
        print(f"Added {len(added)} missing Activepieces setting(s) to {env_path.name}.")
    else:
        print(f"Activepieces settings already present; {env_path.name} unchanged.")
