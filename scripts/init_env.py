#!/usr/bin/env python3
"""Generate secrets once; never overwrite an existing configuration."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parents[1]
target = root / ".env"
content = (root / ".env.example").read_text()
lengths = {
    "MONGO_PASSWORD": 32, "POSTGRES_PASSWORD": 32,
    "LIGHTRAG_API_KEY": 32, "MCP_TOKEN": 32,
    "CREDS_KEY": 32, "CREDS_IV": 16,
    "JWT_SECRET": 48, "JWT_REFRESH_SECRET": 48,
}
for key, size in lengths.items():
    content = content.replace(f"{key}=GENERATE", f"{key}={secrets.token_hex(size)}")
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env esiste già: nessuna modifica effettuata.")
with os.fdopen(fd, "w") as f:
    f.write(content)
print("Creato .env. Inserisci CHAT_API_KEY e KNOWLEDGE_API_KEY.")
