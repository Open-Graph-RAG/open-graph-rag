# Local Ollama Embeddings Checklist

- [x] Select `bge-m3` embeddings via authorization to implement the plan.
- [x] Verify Ollama service, LightRAG compatibility, and existing index state.
- [x] Add pinned Ollama and model initialization services to the stack.
- [x] Pull the model and verify container embedding requests return 1,024 values.
- [x] Checkpoint: serving and embedding compatibility confirmed.
- [x] Configure LightRAG endpoint, model, dimension, and isolated storage/workspace.
- [x] Validate Compose configuration and preserve rollback settings/index.
- [x] Recreate LightRAG and ingest a small sample into the isolated index.
- [x] Verify relevant retrieval, run bridge unit tests, and run MCP smoke check.
- [x] Document setup, migration, validation results, and rollback.
- [x] Checkpoint: end-to-end retrieval works before bulk reingestion.
- [ ] Bulk reingestion: auto-review blocked external extraction of the 9.3-million-character corpus; explicit user approval pending.
