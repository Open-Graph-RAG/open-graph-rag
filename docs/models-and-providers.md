# Models, providers, and embedding migrations

Three roles are configured separately in `.env`:

- `CHAT_*`: the model that chats in LibreChat and uses MCP.
- `KNOWLEDGE_*`: the model LightRAG uses for extraction and keywords.
- `EMBEDDING_*`: the model that creates vectors, with a matching dimension.

The default embeddings use `bge-m3` on Ollama in the stack, through
`http://ollama:11434/v1`, with 1024 dimensions. Ollama does not publish ports
on the host and does not modify any Ollama installation already on your
computer. `BAAI/bge-reranker-v2-m3` is a reranker; it does not create the
embeddings this configuration requires. Reranking remains disabled.

Chat, extraction, and keywords still use OpenAI endpoints: **these calls use
external APIs**, may send text and queries to the configured providers, and
may incur a cost. To make these models local too, configure OpenAI-compatible
endpoints reachable from the containers, suitable models, and placeholder keys
if the local server requires them.

Inside a container, `localhost` refers to that container. If the provider is
another Docker service, use its network name. For a provider on the Linux host,
add `extra_hosts: ["host.docker.internal:host-gateway"]` to the services that
need to reach it; on Docker Desktop, the name is normally available.

Do not change the embedding model or dimension on an already populated index.
`LIGHTRAG_WORKSPACE=company_bge_m3` selects a new workspace; LightRAG 1.5.7
also separates vector tables by model and dimension. Documents from the old
`company` workspace do not appear automatically in the new one: reindex the
original texts. `EMBEDDING_SEND_DIM=false` and `EMBEDDING_USE_BASE64=false` make
requests compatible with Ollama.

## Migration and rollback

For an existing installation, keep the old `.env` in a Git-ignored location
and set the endpoint, placeholder key, model, dimension, and workspace as in
`.env.example`. Recreate LightRAG after verifying Ollama:

```bash
docker compose up -d ollama
docker compose run --rm ollama-init
docker compose exec ollama ollama list
docker compose up -d lightrag
docker compose exec mcp python smoke.py
```

Upload a small document and verify a search before reindexing everything.
CPU configuration does not require a GPU; indexing time depends on CPU, RAM,
and document length.

To return to the old index, restore the previous endpoint/key/model/dimension
and set `LIGHTRAG_WORKSPACE=company`, then run
`docker compose up -d lightrag`. The old `.env` may not contain the workspace;
add it explicitly. Keep the volumes; do not use `down -v`.

Store saved configuration and exported originals in a Git-ignored backup
location; they may contain credentials or confidential data.

[Back to installation](../README.md#installation)
