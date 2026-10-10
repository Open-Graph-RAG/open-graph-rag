# Isolated LightRAG A/B environment recipe

`compose.validation.yaml` is a standalone validation-only Compose file; it is not
included by the deployed stack. The two LightRAG containers use the same pinned
image, file-backed storage implementations, Ollama chat/embedding settings, and
frozen input mount. Their working directories are separate writable clones made
from a sanitized snapshot manifest. No ports are published to the host. The only
named volume is a project-local Ollama model cache; there are no `external:`
volumes and no production database/network dependency.

This recipe has not been started or integration-tested. It is infrastructure
wiring, not evidence of retrieval parity or product behavior. The LiveAdapter
remains fail-closed until a reviewed equivalence attestation, explicit model and
retrieval config, allowed host lists, nonzero provider prices, and credentials
are supplied. Direct generation calls require `--approve-paid`. Do not use the
compose file with deployment paths or real/private sources; use only isolated,
synthetic or specifically authorized snapshots.

Suggested operator sequence (manual, not automatically executed):

1. Use `ogr-product-validation prepare` with a pre-existing sanitized
   `snapshot-manifest.json`; set `OGR_VALIDATION_SNAPSHOT_ROOT` to its output.
2. Set `OGR_VALIDATION_INPUTS_ROOT` to the frozen synthetic input snapshot,
   `OGR_VALIDATION_RUNS_ROOT` to the ignored `.runs` directory, and set a fresh
   validation-only LightRAG key in a private shell environment.
3. Start only this file under a dedicated Compose project name. Check health and
   verify both containers mount the expected distinct snapshot directories.
4. Load pinned local Ollama model revisions explicitly, then create and freeze
   the environment-equivalence attestation. Keep `live` empty until all controls
   are reviewed. A configured paid run remains a separate deliberate action.
5. Stop only the dedicated validation project. Never use `down -v`.

Example isolated invocation:

```sh
docker compose -p ogr-validation -f validation/product/environments/compose.validation.yaml up -d
```

This command is documentation only; this implementation did not run it. The
Compose file exposes the internal service ports only to its private bridge
network. Do not attach unrelated services to that network.
