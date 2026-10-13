# Eng2SQL Agent

Open a SQLite, CSV or Excel file, describe what you want in plain English,
and the agent generates SQL, shows it to you, and — for anything that
changes data — previews exactly what will be affected before it touches
your database.

## How it works

```
Open file → schema extracted → you type English →
follow-up resolved against the conversation → relevant tables retrieved →
Groq LLM generates SQL (grounded in your schema) → validated →
  READ  → executes immediately: answer, chart, SQL, results
  WRITE → shows SQL + "this will affect N rows" + sample rows →
           you confirm → automatic backup → executes in one transaction
```

## Features

- **Accounts** — sign up / sign in; every database, its history and its
  backups are visible only to the account that owns them.
- **AI Chat** — grounded answers, automatic bar/line/pie/scatter/KPI charts,
  and conversation memory scoped to each database ("only the top 2 of those").
- **SQL Workspace** — write SQL yourself; it goes through the same safety
  gate and confirmation flow as AI-generated SQL.
  Includes a SQL formatter.
- **Explorer** — multiple databases (rename, delete, download), schema tree,
  paginated table preview. Click a column header to sort the rows shown.
- **Undo and restore** — every change is backed up first; undo it with one
  click, or restore any earlier backup from the database's menu.
- **Query history** — persistent, searchable, filterable, with a detail view,
  starred queries, and one-click "reuse question" / "open in SQL Workspace".
- **Import / export** — CSV/XLSX import into a table (new, append or
  replace), CSV/XLSX export of any table or query result, and download of
  the whole `.sqlite` file.
- **Persistent** — databases, history and conversation memory survive a
  server restart.
- Responsive UI with light and dark themes.

## Safety measures built in

- **Every generated SQL statement is shown to you** — nothing runs silently.
- **Writes always require explicit confirmation.** Nothing is auto-executed.
- **UPDATE/DELETE without a WHERE clause is rejected outright** — this blocks
  the most common catastrophic mistake before it ever reaches your database.
- **Only `SELECT`, `INSERT`, `UPDATE`, `DELETE` and `CREATE TABLE/VIEW/INDEX`
  are allowed.** `DROP`, `TRUNCATE`, `ALTER`, `ATTACH`, `PRAGMA`, `VACUUM`,
  `REPLACE`, triggers and everything else are blocked. Statements are
  classified by parsing them (SQLGlot), not by keyword matching.
- **Reads run on a query-only connection** with a time limit
  (`SQL_TIMEOUT_MS`) and a row cap.
- **A batch runs as one transaction** — all statements succeed or all roll back.
- **Automatic backup before every write, import or restore**, stored in
  `backups/` (the newest `MAX_BACKUPS_PER_DB` per database are kept).
- **Sign-in required for everything but the login page.** Passwords are
  stored as salted scrypt hashes; sessions use an HttpOnly, SameSite cookie
  whose token is only stored hashed; repeated failed sign-ins are locked
  out temporarily; cross-site requests are rejected.
- The model is asked to flag ambiguous requests (`needs_clarification`)
  rather than guess at intent.
- Upload size limit, SQLite file-header check, strict Content-Security-Policy,
  and no cross-origin API access unless `ALLOWED_ORIGINS` is set.

This is a solid prototype foundation, not a production security boundary —
see **Limitations** below before using it on anything you care about.

## Setup

```bash
pip install -r requirements.txt        # ideally inside a virtualenv
cp .env.example .env                    # then set GROQ_API_KEY
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** (through the server — not the `.html` file)
and create your account.

The **first account** created becomes the owner of any databases that were
already registered. Once everyone who needs an account has one, set
`ALLOW_SIGNUP=false` in `.env` to stop further registrations.

The server starts without a `GROQ_API_KEY`; only AI Chat needs it. All
settings are documented in `.env.example`.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests need no API key or network and run in a temporary folder.

## API

Everything except `/health` and `/auth/*` needs the session cookie set by
signing in.

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | Liveness check |
| GET  | `/auth/status` | Who is signed in; whether sign-up is open |
| POST | `/auth/signup`, `/auth/signin`, `/auth/signout` | Account and session |
| POST | `/auth/password` | Change password (signs out other devices) |
| GET  | `/databases` | List loaded databases |
| POST | `/upload` | Upload `.db`/`.sqlite`/`.sqlite3`/`.csv`/`.xlsx`, get `db_id` + schema |
| POST | `/database/create?name=` | Create an empty database |
| PATCH | `/database/{db_id}` | `{name}` → rename |
| DELETE | `/database/{db_id}` | Delete the database, its history and backups |
| POST | `/table/create` | `{db_id, table_name, columns}` → create a table |
| GET  | `/schema/{db_id}` | The extracted schema |
| GET  | `/table/{db_id}/{table}?page=&limit=` | One page of a table |
| POST | `/query` | `{db_id, question, session_id?}` → answer (read) or confirmation prompt (write) |
| POST | `/sql/execute` | `{db_id, sql}` → same pipeline for hand-written SQL |
| POST | `/sql/format` | `{sql}` → pretty-printed SQL |
| POST | `/confirm` | `{db_id, query_id, confirm}` → run or cancel a pending write |
| GET  | `/backups/{db_id}` | List this database's backups |
| POST | `/backups/{db_id}/restore` | `{name}` → restore a backup (undo) |
| GET  | `/history/{db_id}?search=&status=&starred=&limit=&offset=` | Query history |
| GET  | `/history/detail/{history_id}` | One history record |
| POST | `/history/star/{history_id}` | `{starred}` → star / unstar a query |
| GET  | `/export/table/{db_id}/{table}?format=csv\|xlsx` | Export a table |
| POST | `/export/query` | `{db_id, sql, format}` → export one SELECT |
| POST | `/import/table` | multipart `db_id, table_name, mode, file` → import CSV/XLSX |
| GET  | `/download/{db_id}` | Download the database file |
| POST | `/rag/search` | `{db_id, question}` → the tables retrieved for a question |

### Example: a write (two calls)

```bash
# 0. Sign in once; -c/-b keep the session cookie in a file
curl -c cookies.txt -X POST localhost:8000/auth/signin \
  -H "Content-Type: application/json" -d '{"email": "...", "password": "..."}'

# 1. Ask — returns a query_id and a preview, does NOT execute
curl -b cookies.txt -X POST localhost:8000/query -H "Content-Type: application/json" -d '{
  "db_id": "...",
  "question": "delete cancelled orders from before 2022"
}'
# → {"status": "confirmation_required", "query_id": "...", "previews": [...], ...}

# 2. Confirm — this is the only thing that actually writes
curl -b cookies.txt -X POST localhost:8000/confirm -H "Content-Type: application/json" -d '{
  "db_id": "...", "query_id": "...", "confirm": true
}'
```

## Project layout

```
app/
  main.py        FastAPI routes — orchestration only
  auth.py        Accounts, password hashing, sessions, sign-in routes
  agent.py       LangGraph flow: memory → retrieve → generate → validate → execute → analyse → chart
  llm.py         Groq call + prompt; returns structured SQL as JSON
  rag.py         Schema retrieval (Chroma + MiniLM embeddings), incremental indexing
  db_utils.py    Schema extraction, SQL validation, dry-run previews, execution, backups
  data_tools.py  CSV/XLSX import and export
  history.py     Persistent query history
  memory.py      Per-database conversation memory
  registry.py    Persistent list of known databases
  config.py      Settings and paths (reads .env)
static/          The browser UI (index.html, style.css, script.js, theme.js)
tests/           Backend tests
uploads/         Database files          (gitignored)
backups/         Automatic pre-write backups (gitignored)
data/            Users, history, memory, registry, vector index (gitignored)
```

Schema retrieval only uses the vector index for databases with more than
five tables; smaller schemas are sent to the model whole, so the embedding
model is never loaded for them.

## Limitations (read before using on real data)

- **Accounts are basic.** There is no email verification, password reset or
  admin role — a forgotten password means removing the user from
  `data/users.sqlite`. Serve it over HTTPS (behind a reverse proxy) before
  exposing it beyond localhost, so passwords and cookies aren't sent in the
  clear.
- **Single process.** Pending confirmations live in memory, so they are lost
  on restart (the databases, history and backups are not). Run one worker.
- **SQLite only.** Extending to Postgres/MySQL means swapping the `sqlite3`
  calls in `db_utils.py` and the dialect passed to `sqlglot`.
- **The statement whitelist and WHERE-clause check are the main guardrails.**
  They stop the common failure modes, but for anything beyond a personal
  project also restrict what the database user itself may do.
- **Undo is whole-database.** Restoring a backup rolls back everything
  changed since it was taken, not just one statement.
- **Deleting a database is permanent** — its file, history and backups go
  with it. Download a copy first if in doubt.
