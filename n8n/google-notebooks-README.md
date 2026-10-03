# Import NotebookLM notes into LightRAG

The `workflows/google-notebooks-to-lightrag.json` workflow downloads a Google
Doc exported from NotebookLM as plain text and queues it in LightRAG. The
request includes the Google Doc ID; n8n reads the document through OAuth, so
you do not need to copy its text into the payload. NotebookLM can export notes
to Google Docs. The workflow remains inactive after import.

This workflow does not read private NotebookLM notebooks directly. It uses the
supported Google Docs export and the Google Drive API `files.export` endpoint.

## Setup

1. In NotebookLM, use **Export to Google Docs** for the notes you want to
   import. In n8n, import `workflows/google-notebooks-to-lightrag.json` from the
   **Workflows** section.
2. Assign a **Header Auth** credential named `Notebook ingest webhook auth`
   to the **Authenticated NotebookLM webhook** node. Set the header to
   `X-Ingest-Token` and choose a random token.
3. Create or assign a **Google Drive OAuth2 API** credential to the **Export
   Google Doc as text** node. The authorized Google account must have access to
   the exported document.
4. Assign the **Header Auth** credential `LightRAG X-API-Key` to the **Queue
   exported text in LightRAG** node. Set the header to `X-API-Key` and its value
   to `LIGHTRAG_API_KEY` from `.env`.
5. Save and activate/publish the workflow. The production URL is
   `http://localhost:5678/webhook/google-notebooks-to-lightrag`.

Credentials are stored in the `n8n_data` volume, not in the workflow JSON.
Reconnect the placeholder credential references in the imported JSON.

## Import a document

Send the Google Doc's `driveFileId`. You can get it from the document URL: it
is the part between `/d/` and `/edit`. `notebook` is an optional label;
`file_source` is generated from the stable Google Doc URL.

```bash
curl -X POST http://localhost:5678/webhook/google-notebooks-to-lightrag \
  -H 'Content-Type: application/json' \
  -H 'X-Ingest-Token: YOUR_TOKEN' \
  -d '{"driveFileId":"DOCUMENT_ID","notebook":"Research notes"}'
```

The ID must contain 10 to 200 alphanumeric characters, hyphens, or
underscores. Empty exports and exports over 500000 characters are rejected. An
HTTP `202` means LightRAG accepted the document for background processing; it
may not be searchable yet. The response includes `track_id`. Check it with
`GET http://localhost:9621/documents/track_status/{track_id}`, using the
`X-API-Key` header. An HTTP `409` for an existing source is returned as
`already_present`; other errors fail the execution.
