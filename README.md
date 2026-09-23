# Eng2SQL Agent

Upload a SQLite database, describe what you want in plain English, and the
agent generates SQL, shows it to you, and — for anything that changes data —
previews exactly what will be affected before it touches your database.

## How it works

```
Upload .sqlite → schema extracted → you type English →
Mistral generates SQL (grounded in your schema) → validated →
  READ  → executes immediately, shows SQL + results
  WRITE → shows SQL + "this will affect N rows" + sample rows →
           you confirm → automatic backup → executes in a transaction
```

## Safety measures built in

- **Every generated SQL statement is shown to you** — nothing runs silently.
- **Writes always require explicit confirmation.** Nothing is auto-executed.
- **UPDATE/DELETE without a WHERE clause is rejected outright** — this blocks
  the most common catastrophic mistake before it ever reaches your database.
- **DDL is blocked entirely**: `DROP`, `TRUNCATE`, `ALTER`, `ATTACH`,
  `PRAGMA`, `CREATE`, `GRANT`, `REVOKE`.
- **Only one statement per request** — no chained/injected statements.
- **Automatic backup before every write**, stored in `backups/`, before the
  transaction runs.
- **Full audit log** per database via `GET /history/{db_id}` — every
  statement, its type, and its result.
- The model is asked to flag ambiguous requests (`needs_clarification`)
  rather than guess at intent.

This is a solid prototype foundation, not a production security boundary —
see **Limitations** below before using it on anything you care about.

## Setup

```bash
cd eng2sql
pip install -r requirements.txt --break-system-packages   # or use a venv
cp .env.example .env   # then edit .env and set MISTRAL_API_KEY
uvicorn app.main:app --reload --port 8000
```

`app/llm.py` loads `.env` automatically via `python-dotenv` — no need to
`export` the key manually, just make sure `.env` sits in the project root
next to `requirements.txt`.

Open **http://localhost:8000** — a minimal test UI is served there
(upload a file, type English, confirm/cancel writes).

## API

| Method | Path                | Purpose                                             |
|--------|---------------------|------------------------------------------------------|
| POST   | `/upload`           | Upload a `.sqlite`/`.db` file, get back `db_id` + schema |
| GET    | `/schema/{db_id}`   | View the extracted schema                            |
| POST   | `/query`            | `{db_id, question}` → SQL + results (read) or a confirmation prompt (write) |
| POST   | `/confirm`          | `{db_id, query_id, confirm}` → executes or cancels a pending write |
| GET    | `/history/{db_id}`  | Audit log of everything run against this database    |
| GET    | `/download/{db_id}` | Download the current state of the database file      |

### Example: a read

```bash
curl -X POST localhost:8000/query -H "Content-Type: application/json" -d '{
  "db_id": "...",
  "question": "top 5 customers by total order amount"
}'
```

### Example: a write (two calls)

```bash
# 1. Ask — returns a query_id and a preview, does NOT execute
curl -X POST localhost:8000/query -H "Content-Type: application/json" -d '{
  "db_id": "...",
  "question": "delete cancelled orders from before 2022"
}'
# → {"status": "confirmation_required", "query_id": "...", "affected_count": 2, ...}

# 2. Confirm — this is the only thing that actually writes
curl -X POST localhost:8000/confirm -H "Content-Type: application/json" -d '{
  "db_id": "...", "query_id": "...", "confirm": true
}'
```

## Project layout

```
app/
  main.py       FastAPI routes — upload, query, confirm, schema, history
  llm.py        Mistral API call — forces structured SQL output via tool use
  db_utils.py   Schema extraction, SQL validation, dry-run previews, execution, backups
static/
  index.html    Minimal browser UI for manual testing
uploads/        Uploaded database files (gitignored in practice)
backups/        Automatic pre-write backups
```

## Limitations (read before using on real data)

- **State is in-memory** (`DBS` dict in `main.py`). Restarting the server
  loses all `db_id` mappings and pending confirmations, though the actual
  `.sqlite` files and backups remain on disk. Swap for Redis/Postgres for
  anything long-lived or multi-instance.
- **No authentication/authorization** — anyone who can reach the server can
  upload and query any database. Add auth before exposing this beyond
  localhost.
- **SQLite only** in this prototype. Extending to Postgres/MySQL means
  swapping `sqlite3` calls in `db_utils.py` for SQLAlchemy, and adjusting
  the dialect passed to `sqlglot`.
- **The WHERE-clause and DDL blocks are the main guardrails** — they stop
  the most common failure modes, but a sufficiently adversarial or unusual
  request could still slip through. For anything beyond a personal project,
  also run writes through a DB role with least-privilege access (no DDL
  permissions at all, at the database level, not just app level).
- **No undo button yet** — restoring means manually copying a file back from
  `backups/` over the uploaded database (`restore_db()` in `db_utils.py`
  does this, just not wired to an endpoint).


## Completed product phases

- AI Data Analyst with grounded result explanations
- Automatic bar/line/pie/scatter/KPI visualizations
- Database-scoped persistent conversation memory
- SQL Workspace with the same SQLGlot safety gate
- Persistent query history with search/filter/detail
- CSV/XLSX query/table export and CSV/XLSX import into the active database
- Persistent database registry across server restarts
- Upload/result/export limits and configurable runtime settings
- `/health` endpoint, Dockerfile and docker-compose deployment

### Environment
Copy `.env.example` to `.env` and provide your Mistral key. Never commit `.env`.

### New endpoints
- `GET /history/{db_id}` and `GET /history/detail/{history_id}`
- `GET /export/table/{db_id}/{table_name}?format=csv|xlsx`
- `POST /export/query`
- `POST /import/table`
- `GET /health`
- `GET /download/{db_id}`

### Run
```bash
python -m py_compile app/main.py
python -m py_compile app/agent.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
