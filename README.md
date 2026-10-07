# Document Q&A POC

Upload a file of up to 50 MB, get a summary, and ask follow-up questions.
Streamlit for the UI, Redis 8.0 for working context, SQLite for persistence.

## Run it

```bash
cp .env.example .env      # then add your API key
docker compose up --build
```

Open http://localhost:8501. Supported files: PDF, DOCX, TXT, HTML, CSV, JSON, XLSX.

## How it works

Three containers: `app` (Streamlit), `worker` (background jobs), `redis`.

An upload is saved to the shared volume and queued on a Redis stream. The
worker then takes one of two paths, depending on what the file contains:

| | Prose: PDF, DOCX, TXT, HTML, free-form JSON | Data: CSV, XLSX, JSON records |
|---|---|---|
| Ingest | Extract text, split into ~1,200-character passages, store in SQLite, index in Redis | Load every row into a per-document SQLite database |
| Summarise | Read the text in ~40,000-character sections, take notes on each, merge the notes | Compute statistics for every column, describe them |
| Answer | Find the best-matching passages with Redis full-text search (BM25), answer from those | Have the model write a SELECT, run it read-only, answer from the result |

So the model never receives the whole file. A question costs two or three
model calls regardless of file size.

### What lives where

| Redis (context, disposable) | SQLite (persistence, on the `data` volume) |
|---|---|
| Passages and their search index | Documents and their status |
| Last few conversation turns, 24h TTL | All passages |
| Job queue and live progress | Summaries, full chat history |

Redis runs without persistence on purpose. If it is restarted, passages are
re-indexed from SQLite on the next question, and recent turns are reloaded.

### What is stored in Redis

Six kinds of keys. `{doc}` is the document's ID.

| Key | Type | Contents | Lifetime |
|---|---|---|---|
| `chunk:{doc}:{n}` | Hash | One passage of a prose document: `doc_id`, `seq` (its position), `loc` (such as `p.12` for PDFs), `text` | Until the document is deleted |
| `idx:chunks` | Search index | Full-text index over every `chunk:` hash: `text` for keyword search, `doc_id` to restrict a search to one document, `seq` as a number | Permanent |
| `indexed:{doc}` | String | The passage count, used as a marker that this document's passages are loaded | Until the document is deleted |
| `chat:{doc}` | List | The last 6 question-and-answer turns, each a JSON object with `role` and `content` | Expires 24 hours after the last question |
| `progress:{doc}` | Hash | Live job status for the UI: `stage`, `done`, `total` | Expires after 1 hour |
| `jobs` | Stream | One entry per background job: `type` (`ingest` or `summarize`) and `doc_id`, read by the `workers` consumer group | Entries are never trimmed |

- Data files (CSV, XLSX, record-style JSON) are queried in SQLite, so they have
  no `chunk:` keys; they only ever have `chat:`, `progress:` and job entries.
- The `chunk:` hashes are a second copy of the document text and account for
  nearly all of Redis' memory use.
- Finished jobs are acknowledged but not removed from the `jobs` stream. That is
  harmless for a POC; for longer use, cap the stream with `MAXLEN`.
- The uploaded file, summaries, full chat history and answer details (sources
  and SQL) are in SQLite only.

To inspect a running instance:

```bash
docker compose exec redis redis-cli --scan --pattern '*' | head -20
docker compose exec redis redis-cli FT.INFO idx:chunks
```

## Settings

Set in `.env`; all are optional except the API key.

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic`, or `openai` for any OpenAI-compatible endpoint |
| `LLM_MODEL` | `claude-sonnet-5-5` | Answers and final summaries |
| `LLM_FAST_MODEL` | `claude-haiku-4-5-20251001` | Section notes and search keywords |
| `SUMMARY_MAX_WINDOWS` | `150` | Most sections read per summary; `0` reads everything |
| `SUMMARY_CONCURRENCY` | `4` | Parallel model calls while summarising |
| `TOP_K` | `6` | Passages retrieved per question |
| `CHUNK_CHARS` | `1200` | Passage size |

`SUMMARY_MAX_WINDOWS` is a cost guard. A 50 MB plain-text file is about 1,300
sections, so a full read is about 1,300 model calls. Above the cap, an evenly
spaced sample is read and the UI says what share of the text was covered.

## Limits of this POC

- Retrieval is keyword-based. The model expands each question into search
  terms, which helps, but a question worded very differently from the text
  can miss. Adding embeddings and Redis vector search is the natural next step.
- Scanned PDFs have no text layer and are rejected; there is no OCR.
- Images inside documents are ignored.
- One conversation per document, no user accounts.
- A job that is running when the worker is killed stays marked as in
  progress; delete the document and upload it again.

## Layout

```
app/ui.py           Streamlit pages
app/worker.py       Job loop
app/pipeline.py     Ingest, summarise, answer (start here)
app/parsers.py      File readers, chunking, guarded SQL
app/context.py      Redis
app/persistence.py  SQLite
app/llm.py          Model call (swap providers here)
app/config.py       Settings
```
