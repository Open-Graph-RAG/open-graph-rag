# LibreChat + LightRAG + Postgres + MCP

Stack locale per una knowledge base condivisa. LibreChat è la chat; LightRAG
indicizza documenti e recupera evidenze; il bridge MCP collega i due sistemi.
MongoDB è incluso perché LibreChat lo richiede per utenti e conversazioni:
Postgres non lo sostituisce.

## Avvio

Servono Docker Engine/Docker Desktop con Compose v2 e Python 3 sul computer.
Come punto di partenza, assegna a Docker 8 GB di RAM e almeno 15 GB di disco;
lo spazio necessario cresce con immagini e documenti. Nessuna GPU richiesta
con i provider API preconfigurati.

```bash
cd librechat-lightrag-stack
python3 scripts/init_env.py
```

Apri `.env` e inserisci `CHAT_API_KEY`, `KNOWLEDGE_API_KEY` ed
`EMBEDDING_API_KEY`: possono contenere la stessa chiave. I valori di esempio
non sono credenziali valide. Modifica anche i modelli se il tuo account non ha
accesso a quelli indicati. Non condividere `.env`.

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 lightrag mcp librechat
```

Il primo avvio scarica immagini e crea il database. LightRAG deve diventare
healthy prima dell'avvio di MCP e LibreChat.

| Interfaccia | URL | Accesso |
|---|---|---|
| LibreChat | http://localhost:3080 | Registra il tuo primo account |
| LightRAG | http://localhost:9621/webui | Inserisci il valore di `LIGHTRAG_API_KEY` dalla tua `.env` |

Le porte sono pubblicate solo su `127.0.0.1`. Se avvii lo stack su un server
remoto, usa un tunnel SSH per provarlo dal tuo computer:

```bash
ssh -L 3080:127.0.0.1:3080 -L 9621:127.0.0.1:9621 utente@server
```

Dopo avere creato gli account necessari, puoi impostare
`ALLOW_REGISTRATION=false` in `.env` e applicarlo con:

```bash
docker compose up -d librechat
```

## Prima domanda sui tuoi documenti

1. Apri LightRAG → Documents e carica `sample-data/demo-acme.txt`.
2. Attendi lo stato **processed**. Il caricamento avvia l'indicizzazione,
   che richiede il provider LLM e quello degli embedding.
3. In LibreChat seleziona il modello configurato, apri il menu dei tool/MCP
   e abilita `lightrag`. In alternativa crea un Agent e assegna il tool
   `knowledge_search` al suo modello. Serve un modello che supporti tool calling.
4. Chiedi: «Usa la knowledge base: chi gestisce il contratto Acme Demo e
   quando scade? Cita il documento.»
5. Controlla che compaia la chiamata `knowledge_search`. Le evidenze del demo
   riportano **Giulia Bianchi Demo**, **31 dicembre 2026** e `demo-acme.txt`.

Le istruzioni del server chiedono al modello di consultare la knowledge base,
ma non impongono una chiamata per ogni messaggio: verifica la chiamata del tool.
Un eventuale nome come `knowledge_search_mcp_lightrag` è la denominazione
assegnata dal client al medesimo tool.

I documenti vanno caricati nella WebUI LightRAG. Allegare un file alla chat di
LibreChat **non lo aggiunge automaticamente a LightRAG**. Questo stack non
include il RAG API separato di LibreChat e disabilita il relativo file search.
Meilisearch è omesso e la ricerca storica delle conversazioni è disabilitata.

## Chi salva cosa

| Servizio | Dati / ruolo | Persistenza |
|---|---|---|
| LibreChat | UI, conversazioni, account, configurazione agenti | MongoDB; volumi per upload, immagini, log e dati locali |
| LightRAG | Parsing, estrazione di entità/relazioni, indicizzazione e retrieval | Postgres + volumi per input e file di lavoro |
| Postgres 17 + pgvector | Grafo, vettori, chunk/documenti, cache e stato di indicizzazione | Volume `postgres_data` |
| MCP | Traduce `knowledge_search` in `POST /query/data` | Nessun database proprio |
| MongoDB | Archivio applicativo di LibreChat | Volume `mongo_data` |

LightRAG usa `PGKVStorage`, `PGDocStatusStorage`, `PGVectorStorage` e
`PGTableGraphStorage`. Quest'ultimo salva nodi e archi in tabelle PostgreSQL:
non servono Neo4j né Apache AGE. `postgres/init.sql` abilita pgvector al primo
avvio; LightRAG inizializza le proprie tabelle.

La richiesta segue questo percorso:

1. Il modello in LibreChat chiama il tool MCP con una domanda autonoma.
2. MCP chiama LightRAG con modalità `mix`, un budget di contesto e la API key.
3. LightRAG cerca nel grafo e nei vettori e restituisce chunk, relazioni e fonti.
4. Il modello in LibreChat riceve quel JSON e formula la risposta finale.

`/query/data` evita una seconda generazione della risposta finale da parte di
LightRAG. LightRAG può comunque chiamare un LLM per estrarre keyword della
domanda, oltre a usarlo durante l'indicizzazione. Anche gli embedding richiedono
il relativo provider. La ricerca non equivale quindi a zero chiamate API.

## Modelli e provider

Sono separati tre ruoli configurabili in `.env`:

- `CHAT_*`: modello che conversa in LibreChat e usa MCP.
- `KNOWLEDGE_*`: modello usato da LightRAG per estrazione e keyword.
- `EMBEDDING_*`: modello che produce i vettori, con dimensione coerente.

La configurazione iniziale usa endpoint OpenAI e modelli indicati negli esempi
dei progetti. Lo stack applicativo è self-hosted, ma **l'inferenza predefinita
usa API esterne**: testi e query vengono inviati ai provider configurati e le
chiamate possono avere un costo. Per inferenza locale configura endpoint
OpenAI-compatible raggiungibili dai container, modelli adatti e chiavi
eventualmente fittizie se richieste dal server locale.

Nel container `localhost` indica il container stesso. Se il provider è un altro
servizio Docker, usa il suo nome di rete. Per un provider sull'host Linux,
aggiungi `extra_hosts: ["host.docker.internal:host-gateway"]` ai servizi che lo
devono contattare; su Docker Desktop il nome è normalmente disponibile.

Non cambiare modello/dimensione degli embedding su un indice già popolato:
usa un nuovo database/workspace e reindicizza. `text-embedding-3-small` è
configurato con la dimensione standard 1536; `EMBEDDING_SEND_DIM=false` evita
di richiedere un parametro dimensions a provider che non lo supportano.

## Aggiornamenti e scrittura

Caricamenti, rimozioni e modifiche amministrative si fanno in LightRAG.
MCP espone soltanto ricerca: l'LLM di LibreChat non può aggiungere o cancellare
fatti. Non ci sono connettori Drive, email o CRM e non c'è sincronizzazione
schedulata; per averla occorre una pipeline di ingestion dedicata.

Il workspace `company` e la chiave MCP sono condivisi: tutti gli utenti a cui
assegni il tool possono interrogare la stessa knowledge base. Non è una
configurazione con autorizzazioni per documento o isolamento per cliente.
Le credenziali dei database sono quelle inizializzate al primo avvio: cambiare
solo `.env` dopo la creazione dei volumi non cambia le password nei database.

## Verifica del collegamento

Senza chiamate al modello, verifica health e protocollo MCP:

```bash
docker compose exec mcp python smoke.py
```

Dopo aver indicizzato il demo, verifica il recupero reale delle fonti (può usare
API LLM/embedding):

```bash
docker compose exec mcp python smoke.py "Chi gestisce il contratto Acme Demo e quando scade?"
```

Il risultato deve contenere le evidenze del demo. Una risposta HTTP riuscita con
dati vuoti non dimostra che l'indicizzazione sia completa. Il test MCP interno
non verifica la selezione del tool in LibreChat: completa anche il test dalla UI.

Se qualcosa non funziona:

| Sintomo | Controllo |
|---|---|
| LightRAG non diventa healthy | `docker compose logs --tail=150 postgres lightrag`; controlla chiavi e storage |
| Errore provider 401/403 | Chiave API del ruolo corretto e accesso al modello |
| MCP 401 | Stesso `MCP_TOKEN` in LibreChat e MCP; ricrea i due servizi dopo modifiche |
| MCP 421 | Host del bridge diverso da `mcp:8000`; aggiorna la allowlist in `mcp/server.py` |
| LibreChat blocca l'URL interno | `mcpSettings.allowedDomains` deve includere `http://mcp:8000` |
| Nessuna evidenza | Documento processed, workspace corretto, chiamata al tool effettivamente eseguita |
| Registrazione disabilitata | Riabilita temporaneamente `ALLOW_REGISTRATION` e ricrea LibreChat |

## Persistenza e backup

`docker compose down` arresta lo stack conservando i volumi. Non aggiungere
`-v` se vuoi mantenere i dati: elimina i volumi del progetto.

Per un backup completo conserva `.env` in modo sicuro, i file di configurazione
e tutti i volumi elencati in `compose.yaml`. Per ottenere dump dei database
coerenti con i file, ferma prima le applicazioni:

```bash
mkdir -p backups
docker compose stop librechat mcp lightrag
docker compose exec -T postgres pg_dump -U lightrag -d lightrag -Fc > backups/lightrag.dump
docker compose exec -T mongodb sh -c 'mongodump --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive --gzip' > backups/librechat.archive.gz
# Salva anche i volumi dei file con il tuo sistema di backup Docker.
docker compose start lightrag mcp librechat
```

## Versioni e verifiche effettuate

Preparato il 3 ottobre 2026. Le immagini di LightRAG v1.5.7, LibreChat,
MongoDB 8.0.20 e pgvector/Postgres sono fissate al digest del manifest verificato.
LibreChat usa il canale ufficiale `librechat-dev` del Compose upstream, congelato
al digest incluso: non viene presentato come una release stabile. Il bridge usa
MCP Python SDK 1.26.0; le dipendenze Python risolte sono fissate nel lock file.

Sono stati verificati il file Compose con il JSON Schema ufficiale, i riferimenti delle immagini e il
bridge con test di protocollo MCP, autenticazione, limiti degli argomenti,
conservazione delle fonti e gestione degli errori. I test usano un backend
LightRAG simulato. Il comando `docker compose config` va eseguito sul tuo host;
qui la verifica Compose è statica. **Lo stack completo non è stato avviato qui**, perché questo
ambiente non dispone di un motore Docker; le chiamate al tuo provider e il
percorso completo dalla UI richiedono la verifica sul tuo computer.

Per rieseguire i test del bridge senza Docker:

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.lock
.venv/bin/python -m unittest discover -s mcp/tests -v
```

## Riferimenti ufficiali

- [LightRAG API e storage, v1.5.7](https://github.com/HKUDS/LightRAG/blob/v1.5.7/docs/LightRAG-API-Server.md)
- [LightRAG variabili, v1.5.7](https://github.com/HKUDS/LightRAG/blob/v1.5.7/env.example)
- [LightRAG query API, v1.5.7](https://github.com/HKUDS/LightRAG/blob/v1.5.7/lightrag/api/routers/query_routes.py)
- [LibreChat Compose upstream](https://github.com/LibreChat-AI/LibreChat/blob/main/docker-compose.yml)
- [LibreChat MCP](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers)
- [LibreChat restrizioni MCP](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_settings)
- [MCP Python SDK 1.26.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)

Questo bundle contiene configurazione e bridge; i progetti upstream mantengono
le proprie licenze. Per esporlo su Internet servono un deployment con HTTPS,
domini e controllo degli accessi configurati per il tuo ambiente.
