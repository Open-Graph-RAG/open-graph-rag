# Importare pagine web in LightRAG

Per le note esportate da NotebookLM in Google Docs, consulta
[il workflow NotebookLM](google-notebooks-README.md).

Il workflow `workflows/internet-to-lightrag.json` accetta un prompt, cerca fino
a dieci risultati con Tavily e invia a LightRAG soltanto le pagine per cui
Tavily restituisce `raw_content`. Non usa gli snippet dei risultati come
contenuto sostitutivo. L'importazione è manuale: il workflow è inattivo dopo
l'importazione e non esegue ricerche programmate.

## Configurazione

1. Accedi a n8n su `http://localhost:5678`, crea l'account proprietario e
   importa `internet-to-lightrag.json` dalla sezione **Workflows**.
2. Crea due credenziali **Header Auth** e una **Bearer Auth** per Tavily e assegnale ai nodi indicati:

   | Credenziale | Header | Valore |
   |---|---|---|
   | `Internet ingest webhook auth` | `X-Ingest-Token` | Un token casuale scelto da te |
| `Tavily bearer API key` (Bearer Auth) | `Authorization` automatico | Solo `<TAVILY_API_KEY>`, senza prefisso `Bearer` |
   | `LightRAG X-API-Key` | `X-API-Key` | Valore `LIGHTRAG_API_KEY` del file `.env` |

   La prima protegge il webhook in ingresso. Le altre due tengono le chiavi
   fuori dal JSON del workflow esportato. Le credenziali sono conservate nel
   volume `n8n_data`.
3. Salva e attiva/pubblica il workflow. Il JSON importato contiene riferimenti
   segnaposto alle credenziali, che n8n richiede di ricollegare.

## Invio di una richiesta

Con il workflow attivo, invia una richiesta dal computer che esegue Docker:

```bash
curl -X POST http://localhost:5678/webhook/internet-to-lightrag \
  -H 'Content-Type: application/json' \
  -H 'X-Ingest-Token: IL_TUO_TOKEN' \
  -d '{"prompt":"documentazione aggiornata su PostgreSQL 17","maxResults":5}'
```

`prompt` è obbligatorio. `maxResults` è facoltativo, predefinito a 5 e limitato
all'intervallo da 1 a 10 (numero intero JSON). Il prompt può contenere fino a
4000 caratteri; le pagine oltre 500000 caratteri vengono scartate. La risposta riporta le pagine messe in coda, quelle
già presenti con la stessa sorgente e i `trackIds` restituiti da LightRAG.
Una risposta `202` significa che LightRAG ha accettato i documenti per
l'elaborazione in background; non significa che siano già indicizzabili.
Controlla ogni ID con `GET
http://localhost:9621/documents/track_status/{track_id}` autenticandoti con
`X-API-Key`. Un HTTP `409` per una sorgente già presente viene contato come
già presente; gli altri errori di LightRAG fanno fallire l'esecuzione.

## Deduplicazione e limiti

Il nodo **Validate search results** controlla la risposta di Tavily, scarta
risultati malformati, URL non validi, pagine HTML non estratte e risposte di
accesso negato. Una risposta del provider malformata fa fallire il workflow.
**Prepare graph documents** aggiunge metadati di provenienza agli item n8n,
senza modificare testo o sorgente inviati a LightRAG. LightRAG riceve ancora
`text` e `file_source` ed estrae entità e relazioni con il modello configurato.
Questi controlli verificano il formato dei risultati, non la veridicità dei fatti.

Il workflow normalizza gli spazi e Unicode NFKC e rimuove i contenuti identici
nella stessa esecuzione. LightRAG applica anche la propria deduplicazione
persistente sul contenuto durante l'elaborazione; quindi una risposta in coda
non garantisce un nuovo documento nello stato `processed`. La sorgente URL è
salvata separatamente dal contenuto. Se una sorgente esiste già, LightRAG può
rifiutare un nuovo invio della stessa URL anche quando la pagina è cambiata.
Per aggiornare quel documento, rimuovi prima la versione esistente tramite la
WebUI/API di LightRAG e invia nuovamente il workflow.

Il flusso usa i contenuti grezzi che Tavily riesce a estrarre: non esegue una
crawl ricorsiva, non aggira paywall e non fa deduplicazione semantica. Tavily
può addebitare la ricerca; LightRAG usa i provider LLM ed embedding configurati
per processare i documenti.

## Verifica offline

Esegui `node n8n/tests/workflow.test.cjs` dalla directory principale per
verificare validazione, deduplicazione e gestione delle risposte simulate.
Questi test non sostituiscono l'importazione e una prova nello stack in esecuzione.
