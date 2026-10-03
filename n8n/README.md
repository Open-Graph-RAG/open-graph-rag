# Import web pages into LightRAG

For a self-contained demo with simulated Jira and Figma sources, see the
[product knowledge demo](../sample-data/product-knowledge-demo/README.md).

For notes exported from NotebookLM to Google Docs, see the
[NotebookLM workflow](google-notebooks-README.md).

The `workflows/internet-to-lightrag.json` workflow accepts a prompt, searches
for up to ten results with Tavily, and sends only pages for which Tavily
returns `raw_content` to LightRAG. It does not substitute result snippets for
page content. Import is manual: the workflow is inactive after import and does
not run scheduled searches.

## Setup

1. Open n8n at `http://localhost:5678`, create the owner account, and import
   `internet-to-lightrag.json` from the **Workflows** section.
2. Create two **Header Auth** credentials and one **Bearer Auth** credential
   for Tavily, then assign them to the indicated nodes:

   | Credential | Header | Value |
   |---|---|---|
   | `Internet ingest webhook auth` | `X-Ingest-Token` | A random token you choose |
   | `Tavily bearer API key` (Bearer Auth) | `Authorization` (set automatically) | `<TAVILY_API_KEY>` only, without the `Bearer` prefix |
   | `LightRAG X-API-Key` | `X-API-Key` | The `LIGHTRAG_API_KEY` value from `.env` |

   The first credential protects the incoming webhook. The other two keep
   their keys out of the exported workflow JSON. Credentials are stored in the
   `n8n_data` volume.
3. Save and activate/publish the workflow. The imported JSON contains
   placeholder credential references that n8n requires you to reconnect.

## Send a request

With the workflow active, send a request from the computer running Docker:

```bash
curl -X POST http://localhost:5678/webhook/internet-to-lightrag \
  -H 'Content-Type: application/json' \
  -H 'X-Ingest-Token: YOUR_TOKEN' \
  -d '{"prompt":"current PostgreSQL 17 documentation","maxResults":5}'
```

`prompt` is required. `maxResults` is optional, defaults to 5, and must be a
JSON integer from 1 to 10. The prompt can contain up to 4000 characters; pages
over 500000 characters are discarded. The response reports pages queued,
sources already present, and the `trackIds` returned by LightRAG. An HTTP `202`
means LightRAG accepted the documents for background processing; they may not
be searchable yet. Check each ID with
`GET http://localhost:9621/documents/track_status/{track_id}`, authenticated
with `X-API-Key`. An HTTP `409` for an existing source is counted as already
present; other LightRAG errors fail the execution.

## Deduplication and limits

The **Validate search results** node checks Tavily's response and discards
malformed results, invalid URLs, unextracted HTML pages, and access-denied
responses. A malformed provider response fails the workflow. **Prepare graph
documents** adds provenance metadata to n8n items without changing the text or
source sent to LightRAG. LightRAG still receives `text` and `file_source` and
extracts entities and relationships using its configured model. These checks
validate the result format, not the truth of its claims.

The workflow normalizes whitespace and Unicode NFKC and removes identical
content within each run. LightRAG also deduplicates content persistently while
processing it, so a queued response does not guarantee a new document reaches
the `processed` state. The source URL is stored separately from the content. If
a source already exists, LightRAG may reject the same URL even when the page
has changed. To update that document, remove the existing version through the
LightRAG WebUI/API, then submit the workflow again. The workflow uses the raw
content Tavily can extract: it does not crawl linked pages, bypass paywalls, or
perform semantic deduplication. Tavily search may incur charges; LightRAG
processes documents with the configured LLM and embedding providers.

## Offline verification

Run the workflow unit tests from the repository root:

```bash
node --test n8n/tests/*.test.cjs
```

These mocked tests check web and NotebookLM validation, deduplication, response
handling, and the fictional demo's document payloads and workflow structure.
They do not replace importing the workflow and testing it in the running stack.
