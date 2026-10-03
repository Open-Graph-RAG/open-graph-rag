<p align="center">
  <img src="docs/assets/logo.png" alt="Open Graph RAG logo: a chat bubble surrounding a document and connected knowledge graph" width="240">
</p>

# Open Graph RAG

LibreChat + LightRAG + n8n + PostgreSQL + MCP

A shared knowledge base stack. LibreChat provides the chat interface; LightRAG
indexes documents and retrieves evidence; the MCP bridge connects the two.
n8n provides an interface for creating automations that call LightRAG.
MongoDB is included because LibreChat requires it for users and conversations;
Postgres does not replace it.

## Start here

- [Install the stack](#installation)
- [Run your first knowledge query](#first-query)
- [Follow the daily user workflow](#user-guide)
- [Import content with n8n](#n8n-automations)
- [Troubleshoot the connection](#verify-the-connection)
- [Manage persistence and backups](#persistence-and-backups)

The normal workflow is **collect documents → index in LightRAG → ask in
LibreChat → inspect the cited sources**. n8n is optional for collecting content;
you can start with a single uploaded text document.

## Installation

### 1. Check prerequisites

You need Git, Docker Engine/Docker Desktop with Compose v2, and Python 3 on your
computer. As a starting point, allocate 8 GB of RAM and at least 15 GB of disk
space to Docker; the required space grows with images and documents. No GPU is
required for local embeddings: Ollama uses the CPU by default.

Check that Docker is running and the required commands are available:

```bash
git --version
python3 --version
docker --version
docker compose version
docker info
```

You also need access to an OpenAI-compatible chat/extraction provider. The
example configuration uses OpenAI for those calls and local Ollama embeddings.
Ports `3080`, `9621`, and `5678` must be available; change their values in `.env`
if another application uses them.

### 2. Clone and generate configuration

```bash
git clone https://github.com/chiora93/open-graph-rag.git
cd open-graph-rag
python3 scripts/init_env.py
```

The script creates a Git-ignored `.env` with unique local secrets and refuses
to overwrite an existing file. If you already have a checkout and `.env`, keep
that configuration and continue with the next step. Do not copy the
`GENERATE` values from `.env.example` directly into a working configuration.

### 3. Configure the providers

Open `.env` and enter `CHAT_API_KEY` and `KNOWLEDGE_API_KEY`:
they can use the same key. Embeddings use Ollama in the stack with
`EMBEDDING_API_KEY=ollama` (a placeholder, not a credential). Example values
are not valid credentials. Change the models too if your account does not have
access to the ones shown. Do not share `.env`.

| Setting | Purpose | Default/example |
|---|---|---|
| `CHAT_API_KEY`, `CHAT_API_BASE`, `CHAT_MODELS` | LibreChat's answering model; it must support tool calling | OpenAI, `gpt-4.1-mini` |
| `KNOWLEDGE_API_KEY`, `KNOWLEDGE_API_BASE`, `KNOWLEDGE_MODEL` | LightRAG document extraction and query processing | OpenAI, `gpt-4.1-mini` |
| `EMBEDDING_API_BASE`, `EMBEDDING_MODEL`, `EMBEDDING_DIM` | Local document/query embeddings | Ollama, `bge-m3`, `1024` |
| `LIGHTRAG_API_KEY` | Generated local key for the LightRAG UI/API and n8n | Keep the generated value |
| `MCP_TOKEN` | Generated authentication between LibreChat and the bridge | Keep the generated value |

Leave the generated database and application secrets in place. See
[models and providers](docs/models-and-providers.md) for alternative providers and
embedding migrations. Indexing and chat can incur provider charges even though
embeddings run locally.

### 4. Start and check the stack

Run all Compose commands from the repository root:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 ollama lightrag mcp librechat n8n
```

The first startup downloads images, creates the database, and downloads
`bge-m3` (about 1.2 GB) through the temporary `ollama-init` service. The model
remains in the `ollama_data` volume; `ollama-init` exits with code 0 when it
finishes. LightRAG waits for the download and must become healthy before MCP
and LibreChat start.

In `docker compose ps`, persistent services should be running, and services
with health checks should become healthy. The completed `ollama-init` container
is expected to stop; use `docker compose ps -a` to inspect it. If startup stalls,
see [troubleshooting](#verify-the-connection).

Check the bridge without making model calls:

```bash
docker compose exec mcp python smoke.py
```

### 5. Open the applications

The URLs below use the default ports. If you changed them in `.env`, use those
ports in your browser and SSH tunnel too.

| Interface | URL | Access |
|---|---|---|
| LibreChat | http://localhost:3080 | Register your first account |
| LightRAG | http://localhost:9621/webui | Enter the `LIGHTRAG_API_KEY` value from your `.env` |
| n8n | http://localhost:5678 | Create the owner account on first access |

Ports are published only on `127.0.0.1`. If you run the stack on a remote
server, use an SSH tunnel to access it from your computer:

```bash
ssh -L 3080:127.0.0.1:3080 -L 9621:127.0.0.1:9621 -L 5678:127.0.0.1:5678 user@server
```

After registering the intended LibreChat users, you can set
`ALLOW_REGISTRATION=false` in `.env` and apply it with:

```bash
docker compose up -d librechat
```

## First query

1. Open LightRAG → Documents and upload `sample-data/demo-acme.txt`.
2. Wait for the **processed** status. Uploading starts indexing, which requires
   the LLM and embedding providers.
3. In LibreChat, select the configured model, open the tools/MCP menu, and
   enable `lightrag`. Alternatively, create an Agent and assign the
   `knowledge_search` tool to its model. The model must support tool calling.
4. Ask: “Use the knowledge base: who manages the Acme Demo contract, and when
   does it expire? Cite the document.”
5. Check that the `knowledge_search` call appears. The demo evidence gives
   **Giulia Bianchi Demo**, **December 31, 2026**, and `demo-acme.txt`.

The server instructions ask the model to consult the knowledge base, but do not
require a tool call for every message: check that the tool was called. A name
such as `knowledge_search_mcp_lightrag` may be the client-assigned name for the
same tool.

Documents must be uploaded through the LightRAG WebUI. Attaching a file to a
LibreChat conversation **does not automatically add it to LightRAG**. This
stack does not include LibreChat's separate RAG API and disables its related
file search. Meilisearch is omitted, and conversation history search is
disabled.

## User guide

### 1. Collect and upload source material

Use LightRAG's **Documents** page at `http://localhost:9621/webui` to manage
knowledge sources. Start with small, readable text files and keep meaningful
filenames so citations are easy to recognize. For other formats, check what the
installed WebUI accepts and inspect the extracted content before relying on it.

Upload documents relevant to the questions you want to answer. Uploads enter a
processing queue: wait for **processed**, rather than assuming an accepted
upload is already searchable. For failed documents, inspect their status and
`docker compose logs --tail=150 lightrag` before retrying. Larger collections
can take time on CPU embeddings and use the extraction provider's API.

### 2. Connect a LibreChat conversation to the knowledge base

Sign into LibreChat, select a tool-capable model, and enable the `lightrag` MCP
server in the tools menu. If using an Agent, assign its `knowledge_search` tool
and save the Agent configuration. Check tool availability when starting a new
conversation or changing models.

The optional [Product Knowledge agent setup](docs/agent-mcp-setup.md) describes
an agent that also uses Jira and Figma. Those external integrations require
each user's OAuth connection; they are not needed for ordinary document search.

### 3. Ask a specific question and check the evidence

Include the product, project, customer, or document name in your question, and
request citations. For example:

> Use the knowledge base to summarize the Acme Demo contract owner and expiry
> date. Cite the source document and identify any missing information.

Expand the tool activity and confirm `knowledge_search` ran. Then check that
the answer's source names match the uploaded documents and that the cited
content supports the claims. A fluent answer alone does not prove the model
searched the knowledge base. If no evidence is found, check document status,
the configured workspace, and the tool call before uploading more material.

For follow-up questions, name the subject again when ambiguity is possible.
Ask the model to distinguish documented facts, conflicting sources, and open
questions. The bridge returns evidence; the LibreChat model writes the answer.

### 4. Maintain the knowledge base

Manage document replacement and deletion in LightRAG. The MCP bridge is
read-only, so asking LibreChat to update a fact does not update the index.
When a source changes, review/remove the previous version through LightRAG and
upload or ingest the replacement, then wait for processing and repeat a known
query. Keep originals outside the stack for recovery and reindexing.

All users with access to this MCP tool query the same configured workspace.
Use this stack for a shared knowledge base; it does not enforce per-document
permissions. See
[access and data boundaries](#access-and-data-boundaries) for the limits.

### 5. Stop and resume

```bash
# Stop while retaining containers and data.
docker compose stop
# Resume the existing containers.
docker compose start
# Apply configuration changes or recreate removed containers.
docker compose up -d --build
```

`docker compose down` also keeps data volumes. `docker compose down -v` deletes
them. See [persistence and backups](#persistence-and-backups) before maintenance.

## Product knowledge demo

The [Jira and Figma demo](sample-data/product-knowledge-demo/README.md) uses
fictional sources to explain two connected products and prepare a cross-team
initiative. It includes an unresolved design/requirement conflict, missing
policy ownership, a manual n8n ingestion workflow, and guided evaluation prompts.
See the [concept](docs/ideas/product-knowledge-demo.md) for scope and assumptions.

The [Product Knowledge agent](docs/agent-mcp-setup.md) combines LightRAG retrieval
with selected read-only Jira and Figma MCP tools. External sources require
each user's OAuth authorization; simulated demo sources use LightRAG only.
The agent provisioning script and connection steps are documented in the agent
guide. The demo workflow must be imported and executed separately; wait for
LightRAG indexing before testing the demo questions.

## n8n automations

Use n8n when content should enter LightRAG through a repeatable workflow:

1. Open `http://localhost:5678` and create the n8n owner account.
2. Choose a workflow: [web search ingestion](n8n/README.md),
   [NotebookLM exports](n8n/google-notebooks-README.md), or the
   [fictional product demo](sample-data/product-knowledge-demo/README.md).
3. Import its JSON and follow its guide to connect the required credentials.
   Use `http://lightrag:9621` inside n8n. Configure a Header Auth credential
   with header name `X-API-Key` and the value of your local `LIGHTRAG_API_KEY`.
4. Run the workflow using its documented manual trigger or webhook. Imported
   workflows do not synchronize content automatically; the web-ingestion
   webhook requires activation/publishing and its own authentication token.
5. Inspect the n8n execution result, then verify the documents reach
   **processed** in LightRAG. A queued response is not completed indexing.
6. Ask a source-specific question in LibreChat and verify its citations.

Store provider keys in n8n credentials, not exported workflow JSON. n8n stores
workflows, credentials, and its encryption key in `n8n_data`; include that volume
in backups.

The UI is accessible only from the computer running Docker. Webhooks also
receive requests only from there until you configure an HTTPS reverse proxy
and the public n8n URL. `N8N_PORT` and `N8N_TIMEZONE` can be configured in
`.env`.

## What each service stores

| Service | Data / role | Persistence |
|---|---|---|
| LibreChat | UI, conversations, accounts, agent configuration | MongoDB; volumes for uploads, images, logs, and local data |
| LightRAG | Parsing, entity/relationship extraction, indexing, and retrieval | Postgres + volumes for inputs and working files |
| n8n | Automation workflows and credentials | `n8n_data` volume (SQLite and encryption key) |
| Postgres 17 + pgvector | Graph, vectors, chunks/documents, cache, and indexing status | `postgres_data` volume |
| MCP | Translates `knowledge_search` to `POST /query/data` | No database of its own |
| MongoDB | LibreChat application store | `mongo_data` volume |

LightRAG uses `PGKVStorage`, `PGDocStatusStorage`, `PGVectorStorage`, and
`PGTableGraphStorage`. The latter stores nodes and edges in PostgreSQL tables:
Neo4j and Apache AGE are not needed. `postgres/init.sql` enables pgvector on
first startup; LightRAG initializes its own tables.

Requests follow this path:

1. The model in LibreChat calls the MCP tool with a standalone question.
2. MCP calls LightRAG in `mix` mode, with a context budget and the API key.
3. LightRAG searches the graph and vectors and returns chunks, relationships,
   and sources.
4. The model in LibreChat receives that JSON and writes the final answer.

`/query/data` avoids a second final-answer generation by LightRAG. LightRAG may
still call an LLM to extract query keywords, in addition to using one during
indexing. Embeddings also require their provider. Searching therefore does not
mean zero API calls.

## Access and data boundaries

The configured `LIGHTRAG_WORKSPACE` (default `company_bge_m3`) and MCP key are
shared: all users assigned the tool can query the same knowledge base. This
setup does not provide per-document permissions or customer isolation. The
bridge exposes search only; document administration belongs in LightRAG.

Database credentials are initialized on first startup. Changing `.env` after
volumes have been created does not change the database passwords. See
[models and providers](docs/models-and-providers.md) before changing an embedding
model, dimension, or workspace on a populated installation.

## Verify the connection

Without model calls, check health and the MCP protocol:

```bash
docker compose exec mcp python smoke.py
```

After indexing the demo, verify actual source retrieval (this may use LLM and
embedding APIs):

```bash
docker compose exec mcp python smoke.py "Who manages the Acme Demo contract, and when does it expire?"
```

The result should contain the demo evidence. A successful HTTP response with
empty data does not prove indexing is complete. The internal MCP test does not
verify tool selection in LibreChat: also complete the UI test.

If something does not work:

| Symptom | Check |
|---|---|
| LightRAG does not become healthy | `docker compose logs --tail=150 postgres lightrag`; check keys and storage |
| Provider error 401/403 | Correct role's API key and model access |
| MCP 401 | Same `MCP_TOKEN` in LibreChat and MCP; recreate both services after changes |
| MCP 421 | Bridge host differs from `mcp:8000`; update the allowlist in `mcp/server.py` |
| LibreChat blocks the internal URL | `mcpSettings.allowedDomains` must include `http://mcp:8000` |
| No evidence | Document is processed, workspace is correct, and the tool call actually ran |
| Registration is disabled | Temporarily re-enable `ALLOW_REGISTRATION` and recreate LibreChat |

## Persistence and backups

`docker compose down` stops the stack and keeps the volumes. Do not add `-v` if
you want to keep the data: it deletes the project volumes.

For a complete backup, safely retain `.env`, the configuration files, and all
volumes listed in `compose.yaml`. For database dumps consistent with the files,
stop the applications first:

```bash
mkdir -p backups
docker compose stop librechat mcp lightrag n8n
docker compose exec -T postgres pg_dump -U lightrag -d lightrag -Fc > backups/lightrag.dump
docker compose exec -T mongodb sh -c 'mongodump --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip' > backups/librechat.archive.gz
# Also save the file volumes with your Docker backup system.
docker compose start lightrag mcp librechat n8n
```

## UI branding

Compose mounts the repository logo into LibreChat's login logo and browser
favicons, and LightRAG's workspace welcome logo (`http://localhost:9621/workspace/`
with the default port) and favicon. The mounts are read-only.
`docs/assets/logo.svg` embeds the PNG so the applications can keep their existing
SVG asset paths. This customizes the static logos; other built-in product icons
and names remain as provided by the applications.

Apply branding changes to an existing stack with:

```bash
docker compose up -d --no-deps librechat lightrag
```

Hard-refresh the browser if it has cached the previous images. LightRAG's logo
asset filename is specific to the pinned image in `compose.yaml`; check the
`/app/lightrag/api/webui/assets/logo-*.svg` path when upgrading that image.
If replacing the PNG, regenerate the SVG wrapper with the same image.

## Versions and verification

Prepared October 3, 2026. LightRAG v1.5.7, LibreChat, MongoDB 8.0.20, and
pgvector/Postgres images are pinned to the verified manifest digest. The n8n
image is pinned to version `2.0.0`; its digest was not verified in this
environment.
LibreChat uses the official `librechat-dev` channel from the upstream Compose
file, pinned to the included digest; it is not presented as a stable release.
The bridge uses MCP Python SDK 1.26.0; resolved Python dependencies are pinned
in the lock file.

The Compose file was checked against the official JSON Schema, image
references were checked, and the bridge was tested for MCP protocol,
authentication, argument limits, source preservation, and error handling. The
tests use a mocked LightRAG backend. The initial verification used static
Compose validation; later local verification ran `docker compose config`,
started the stack, and checked sample retrieval with Ollama embeddings, as
recorded in [the implementation notes](tasks/plan.md). The n8n workflows have
offline tests; their full external-provider ingestion paths still need live
verification with the required credentials.

To rerun the bridge tests without Docker:

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.lock
.venv/bin/python -m unittest discover -s mcp/tests -v
```

## Official references

- [LightRAG API and storage, v1.5.7](https://github.com/HKUDS/LightRAG/blob/v1.5.7/docs/LightRAG-API-Server.md)
- [LightRAG environment variables, v1.5.7](https://github.com/HKUDS/LightRAG/blob/v1.5.7/env.example)
- [LightRAG query API, v1.5.7](https://github.com/HKUDS/LightRAG/blob/v1.5.7/lightrag/api/routers/query_routes.py)
- [LibreChat upstream Compose file](https://github.com/LibreChat-AI/LibreChat/blob/main/docker-compose.yml)
- [LibreChat MCP](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers)
- [LibreChat MCP restrictions](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_settings)
- [n8n self-hosting with Docker](https://docs.n8n.io/hosting/installation/docker/)
- [MCP Python SDK 1.26.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)

This bundle contains configuration and the bridge; upstream projects retain
their own licenses. Exposing it to the Internet requires a deployment with
HTTPS, domains, and access controls configured for your environment.
