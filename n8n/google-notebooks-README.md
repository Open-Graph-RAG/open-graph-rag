# Importare note di NotebookLM in LightRAG

Il workflow `workflows/google-notebooks-to-lightrag.json` scarica come testo
semplice un Google Doc esportato da NotebookLM e lo mette in coda a LightRAG.
La richiesta contiene l'ID del Google Doc; n8n lo legge usando OAuth e non
richiede di copiare il testo nel payload. NotebookLM esporta le note in Google
Docs. Il workflow rimane inattivo dopo l'importazione.

Questo flusso non legge direttamente i notebook privati di NotebookLM: si basa
sull'esportazione supportata in Google Docs e sull'API Google Drive `files.export`.

## Configurazione

1. In NotebookLM, usa **Esporta in Google Docs** per le note da importare. In
   n8n, importa `workflows/google-notebooks-to-lightrag.json` dalla sezione
   **Workflows**.
2. Collega al nodo **Authenticated NotebookLM webhook** la credenziale **Header
   Auth** chiamata `Notebook ingest webhook auth`, con header `X-Ingest-Token` e
   un token casuale scelto da te.
3. Crea o collega al nodo **Export Google Doc as text** una credenziale **Google
   Drive OAuth2 API** autorizzata a leggere il Google Doc esportato. L'account
   Google autorizzato deve poter accedere al documento.
4. Collega al nodo **Queue exported text in LightRAG** la credenziale **Header
   Auth** `LightRAG X-API-Key`, con header `X-API-Key` e valore
   `LIGHTRAG_API_KEY` dal file `.env`.
5. Salva e attiva/pubblica il workflow. L'URL di produzione è
   `http://localhost:5678/webhook/google-notebooks-to-lightrag`.

Le credenziali sono conservate nel volume `n8n_data`, non nel JSON del workflow.
I riferimenti segnaposto del JSON importato vanno ricollegati in n8n.

## Importare un documento

Invia il `driveFileId` del Google Doc. Puoi ricavarlo dall'URL del documento,
tra `/d/` e `/edit`. `notebook` è un'etichetta facoltativa; `file_source` viene
creato dal workflow usando l'URL stabile del Google Doc.

```bash
curl -X POST http://localhost:5678/webhook/google-notebooks-to-lightrag \
  -H 'Content-Type: application/json' \
  -H 'X-Ingest-Token: IL_TUO_TOKEN' \
  -d '{"driveFileId":"ID_DOCUMENTO","notebook":"Appunti di ricerca"}'
```

L'ID deve contenere da 10 a 200 caratteri alfanumerici, trattini o underscore.
Gli export vuoti o oltre 500000 caratteri vengono rifiutati. Una risposta `202`
significa che LightRAG ha accettato il documento per elaborarlo in background;
non significa che sia già indicizzabile. La risposta include `track_id`, da
controllare con `GET http://localhost:9621/documents/track_status/{track_id}`
usando l'header `X-API-Key`. Un HTTP `409` per una sorgente già presente viene
restituito come `already_present`; gli altri errori fanno fallire l'esecuzione.
