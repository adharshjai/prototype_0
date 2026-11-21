# Security: what protects what

pcldb takes SQL from people, keeps tokens, and talks to two outside services (the ARC LLM and
Globus Search). This page says what protects each part, what a person can still see, and what is
only good enough for a prototype. Back to the [README](../README.md).

## Expert SQL: read-only by design

People type SQL in the Expert view and with `pcldb sql`. The protection is the database itself,
not a filter on the text. pcldb also checks the text first (not empty, at most
`sql.max_sql_chars` characters, no NUL, the first word is `SELECT`, `WITH`, `VALUES`, `EXPLAIN`,
or on PostgreSQL also `TABLE`), but only to give a clear message; it is not the safety boundary.

On both engines:

- Every query gets a **new connection** that is used once and closed, so nothing a person sets
  can reach the next query.
- A **time limit** (`sql.timeout_seconds`, 15 s), a row limit (`sql.max_rows`, 1000), a cut of
  long cells (`sql.max_cell_chars`), and at most 32 MB of values per answer.
- At most 4 expert queries at once (a fifth waits 3 s, then gets `sql_busy`).
- Each query is written to the private audit table with the user's name, the SQL text and the
  outcome (that is what "Your last queries" shows).
- `pcldb doctor` runs one harmless and five hostile statements through the sandbox and says
  whether all five were stopped.

### PostgreSQL

- Expert SQL runs as its own login role, `pcl_reader`. It is not a member of any other role. It
  may connect, see the schema `pcl` (the catalogue) and read its tables and views. Nothing else.
  The private schema `app` (users, tokens, logs, the search queue) cannot be read by it.
- The rights every role gets by default are taken away in this database: connecting, temporary
  tables, the schema `public`, the languages `plpgsql` and `sql`, and about 180 built-in
  functions (changing settings inside a statement, sleeping, ending or watching other sessions,
  advisory locks, notifications, writing large objects, the activity statistics).
- The role's own settings: read-only transactions, `statement_timeout`, `lock_timeout`, idle
  limits, `temp_file_limit` 256 MB, no parallel workers, `search_path = pcl`, at most 6
  connections, and every statement written to the server's log.
- Each query runs in `BEGIN TRANSACTION READ ONLY` with its own statement timeout, is sent
  through the protocol that accepts exactly one statement, is read row by row up to the limit,
  is cancelled from the client side when the deadline passes, and is rolled back.
- `pcldb doctor` and `/status` ask the database, as the reader itself, whether it could read
  private data or change anything. If so, expert mode is switched off (`sandbox_unsafe`).
- The server writes its messages in its own language (`lc_messages`; a lab server may answer in
  German, for example, and the reader cannot change that). So pcldb never reads the words of a
  message to decide what happened. A parameter mark such as `$1` is told apart from a lost
  connection by the error code, the state of the session (still alive) and the `$n` in the SQL
  text. The private schema is recognised by the error code for a missing right together with
  the schema's name in the error (a name stays as it is in every language); a table that is
  only called `app` in the query keeps the server's own sentence. Both get the same sentence as
  on SQLite. A second statement is recognised by the syntax error code and text after the first
  `;`, `SELECT ... INTO` by the key word `INTO`, a `WITH` part that writes by its `INSERT`,
  `UPDATE`, `DELETE` or `MERGE`, and the time limit by the time that has passed. The server's
  own words are only passed on after "PostgreSQL said:".

**What a reader can still see or do on PostgreSQL** (no setting can hide it):

- The names, column names, comments, sizes and row estimates of the private tables in `app`
  (never a row and never a sample value); the text of view definitions; role names (passwords
  show as stars); the server's version, port and start time; how much the server has written;
  its own cursor; one transaction id per query.
- One statement can make its server process hold a lot of memory until the deadline. Measured
  on PostgreSQL 18.7: about 3 GB within 15 s with ten `repeat('x', 500000000)` columns.
  PostgreSQL has no setting that caps this. The bounds are the time limit, 4 queries at once and
  6 connections.
- A query holds read locks on what it reads until it ends; `pcldb db reset` waits that long.
- Every reader statement is in the server's log (`var/pgdata/log/`): the log grows with use and
  holds the typed SQL.
- On an **external** PostgreSQL whose owner is not a superuser, pcldb cannot take away the
  function rights or the languages, and cannot set `temp_file_limit` or the statement log. Then
  expert SQL still cannot read private data or change anything, but a query can call `pg_sleep`
  and `set_config`, take advisory locks, end other expert queries and fill temporary files until
  its time limit. `pcldb doctor` and the Status view say so in one sentence. pcldb's own local
  database is not affected (its owner is a superuser).

### SQLite

- Each query opens an empty in-memory database and attaches only the catalogue file
  (`pcl_catalog.sqlite`) **read-only**. The private file (`pcl_app.sqlite`: users, tokens,
  logs) is never opened by this connection, so it cannot be read whatever the SQL says.
- Then limits: no further files can be attached, at most 10 MB per value, the SQL length limit,
  `query_only`, no trusted schema, extensions off, and limits on pattern length, expression depth
  and compound queries.
- Deep nesting is refused before SQLite reads the text: brackets at most 100 deep, and at most
  200 `SELECT` key words (sub-queries also nest without brackets, for example a chain of `WITH`
  parts that each read the one before). Strings and comments do not count. SQLite checks the
  depth of an expression only after it has walked it, and a chain of `WITH` parts that each put
  operators on the column of the one before becomes one expression as deep as all of them. So
  each statement runs in a thread of its own with 32 MB of stack, and on SQLite the text may be
  at most 30,000 characters long, whatever `sql.max_sql_chars` says. The depth can never be
  more than the length, and 32 MB held about 72,000 levels when measured (SQLite 3.50 on macOS,
  arm64), so a deeply nested statement ends in a message, not in a crash of the server,
  whatever thread sent it.
- Then an authorizer that allows only reading: `PRAGMA`, `ATTACH`, `VACUUM`, every write,
  transactions and temporary tables are refused. Only the catalogue's own tables and views,
  `sqlite_master` and the JSON table functions may be read, and only functions on an allow-list
  may be called (no `load_extension`).
- A progress check stops a statement at its deadline; a watchdog thread interrupts it as a
  second line. A few functions that could run for minutes in one step (`instr`, `replace`,
  `trim` with a set, `unhex` with a set) are replaced by guarded versions with the same results.

**What a reader can still see or do on SQLite:**

- The catalogue's own table definitions and SQLite's version.
- While a query reads the catalogue, writers of the catalogue wait. A registration may wait up
  to `sql.timeout_seconds` + 5 seconds (20 s by default) behind a slow expert query, and so does
  a request that comes after that registration. Sign-in checks, other reads and the query log do
  not wait for the expert query itself.
- Up to 1 GB of temporary files per statement in the system's temporary folder (removed when it
  ends). If another program fills the same disk during a query, that query can be stopped and
  just has to be run again.
- One call of an allowed function on 10 MB values can run a second or two past the deadline.
- The first expert query caps the memory of every SQLite connection of the process at 256 MB
  (and that cap cannot be raised again). pcldb's own connections use a few MB.

### Both engines

- Timing tells a reader nothing about private rows: no statement can touch them.
- The catalogue includes operator names and notes. Everyone with expert SQL can read them.
- "Download CSV" puts a single quote in front of a cell that starts with `=`, `+`, `-`, `@`, a
  tab or a carriage return, so a spreadsheet program shows it instead of running it as a
  formula; a plain number such as `-5` is left as it is. The command line's `--csv` output of
  `pcldb sql` writes the values unchanged: open such files with care.

## Tokens and sign-in

- A token is `pcl_` and 43 random characters (from Python's `secrets`). Only its SHA-256 is
  stored. The text is never printed: `pcldb token new` saves it in a private file
  `var/tokens/<user>_<first 8 of the id>.txt`, `pcldb setup` saves the admin's in
  `var/admin_token.txt`. Hand the line over and delete the file.
- Tokens made without `--days` do not expire, the admin's included. `pcldb token revoke --id
  TOKEN_ID` stops one for good; `pcldb user disable NAME` stops all of a user's tokens.
- `pcldb serve` prints a one-time sign-in link (`#signin=<32 characters>`). It works once,
  within five minutes, is kept only in memory, and gives a 12-hour token of the user `admin`.
  The part after `#` never reaches the server's log; the page removes it from the address before
  it uses it.
- The website keeps the token in `sessionStorage` (this tab only) and sends it only in the
  `Authorization` header. "Sign out" makes the page forget it; the token itself stays valid until
  it expires or is revoked.
- The server refuses a token in the address (any query parameter named like `token` or `key`),
  so tokens do not end up in browser history or logs.
- The command line never takes a token as an argument (it would show in the process list and the
  shell history). It reads `PCLDB_TOKEN`, else `--token-file`, else `var/admin_token.txt`.
- `pcldb/client.py` follows no redirects and uses no proxy, so a token goes only to the address
  given.

## The website

- The page is three static files and an icon, served from a fixed list of names. No file name is
  ever taken from the address.
- Every response carries this content security policy, so the page can only run its own script
  and talk to its own server:
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
- Also on every response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store`. No CORS headers.
- Everything that comes from the server (notes, sample ids, the assistant's text) is put into
  the page as plain text, never as HTML.
- The server only answers requests whose Host header is `127.0.0.1`, `localhost` or `[::1]` with
  its port (or a name in `server.allowed_hosts`); this stops another web page from reaching it
  under a made-up name.
- "Open in Globus" is offered only for an address on `https://app.globus.org`. The `scp`,
  `sftp` and `rsync` commands are offered only when the path, host and user name hold nothing
  but letters, digits and `. _ / @ + -`.
- The ARC user name typed in the Expert view stays in that browser (`localStorage`) and is never
  sent to the server.

## Secrets and where they live

| Secret | Where | Notes |
|---|---|---|
| Your ARC LLM key | `~/.pcldb/arc_llm_api_key` (Windows: `%USERPROFILE%\.pcldb\arc_llm_api_key`), or the variable `PCLDB_LLM_API_KEY`, or the file named by `llm.api_key_file` | Personal. Outside this folder. Sent only in the `Authorization` header to the ARC LLM address. |
| Your Globus sign-in | `~/.pcldb/globus_tokens.json` | Personal. Written by the Globus SDK. `pcldb search logout` cancels and removes it. |
| Passwords of the local PostgreSQL | `var/local_secrets.json` (private file) | Made by setup. The roles get them as SCRAM verifiers, so no clear password is in a statement or a server log. |
| Connection strings of an external PostgreSQL | the variables `PCLDB_PG_OWNER_DSN`, `PCLDB_PG_APP_DSN`, `PCLDB_PG_READER_DSN` (better than the settings file) | Never shown by pcldb; they read "<hidden>". |
| Tokens | only their SHA-256 in the database; the text once in `var/admin_token.txt` and `var/tokens/` | See above. |

"Private file" means mode 0600 on macOS and Linux. Windows has no such modes: there the files
are only as private as the project folder. Keep the project in your own user profile (for
example `%USERPROFILE%\pcldb`), never directly under `C:\`, where every account on the PC can
read a new folder; see [WINDOWS.md](WINDOWS.md).

`var/`, `.venv/` and `pcldb_config.json` are in `.gitignore`. Nothing secret belongs in
`pcldb_config.json`. pcldb never prints, logs, stores in the database or returns from the API
the LLM key, Globus tokens, database passwords or token texts.

What is written where:

- `var/log/server.log`: one line per request (method, path without the query, status, user name,
  time) and the details of unexpected failures. Never a token, header, question, SQL text or key.
- The private audit tables in the database: every registration (also refused ones), every
  question (`ask`), every search request and every SQL text, with the user's name.
- `pcldb dump` files hold every table: user names, token hashes, the audit tables, notes. Keep
  them private.
- `pcldb export-sqlite` files hold the catalogue only (no users, tokens or logs), but the
  catalogue has operator names and notes.

## What the ARC LLM sees

- Planning a question: the question, today's date and this computer's offset from UTC, the list
  of fields, and the known values of `instrument`, `method`, `completion`, `review_state`,
  `labels`, `tags`, `models` (up to 30 each) and up to 40 recent sample ids, each cut to 64
  characters.
- Writing the answer: the question, the sentence that says how it was searched, the counts, and
  up to `ask.max_results` (20) results with only these fields: folder, instrument, started,
  method, completion, sample_ids, labels, label_counts, tags, models, review_state,
  n_needs_review, items, items_planned, simulated, corrected, problems (values cut to 200
  characters).
- Never: operator names, notes, locations, paths, users, tokens, SQL, files.
- The assistant has no tools. Its plan is checked strictly before anything runs, and the results
  table is always the real data. Its text is shown as plain text. A record whose notes say
  "ignore your instructions" could at worst make the written answer odd.
- The ARC service logs every request under the key owner's name. That is why pcldb uses the key
  of the person running it on their own computer.

## What goes to Globus Search

Nothing goes to Globus Search until someone runs `pcldb search push` (after `pcldb search
login`). Then each session's entry is sent: the fields listed in
[GLOBUS_SEARCH.md](GLOBUS_SEARCH.md), such as instrument, folder, path, dates, sample ids, tags,
labels, method, location and counts. Operator names and notes stay out unless the lab switches
`search.include_operator` or `search.include_notes` on. Globus Search is outside the university's
High Assurance coverage: nothing sensitive belongs in an entry.

Entries are never public: `public` and `all_authenticated_users` are refused. With an empty
`search.visible_to`, an entry is visible only to the Globus identity that pushed it. Anyone who
knows an index id can read the index's name, description and counts (never the entries), so
pcldb gives a new index a bland name.

## What is prototype-grade (the honest list)

- **Plain HTTP on one computer.** The server listens on `127.0.0.1` without HTTPS. Do not start
  it with `--host 0.0.0.0` or add names to `server.allowed_hosts` on a network you do not
  control: tokens would travel unencrypted.
- **One sign-in link and pasted tokens.** There is no per-person sign-in page, no password, no
  single sign-on. Tokens do not expire unless made with `--days`.
- **Personal keys.** The ARC LLM key and the Globus sign-in belong to the person who runs pcldb.
  With the Globus backend, every website user searches with that one person's Globus identity.
- **Every role sees every session's full record** (`GET /session`), operator and notes
  included. There are no per-record access rules.
- **Slow requests hold a server thread.** The server has no cap on its threads. A request body
  may arrive slowly (at least 16 KB in every 30-second window, and all of it within 30 seconds
  plus one second per 16 KB: about 17 minutes for the 16 MB limit). After a refused token the
  server still reads the rest of the body and throws it away, so that the client sees the
  refusal rather than a broken connection. A program on this computer could therefore keep
  threads busy for minutes; the server listens on this computer only.
- **Rate limits live in memory** (questions per person per minute) and start again when the
  server restarts. There is no lockout after wrong tokens (a token has 43 random characters, so
  guessing is not practical).
- **SQLite writers wait for readers** (above), and its memory cap applies to the whole process.
- **The command line's CSV output (`--csv`) is not guarded** against spreadsheet formulas (the
  website's "Download CSV" is).
- **Not checked on Windows** at all; see [WINDOWS.md](WINDOWS.md).
- Memory safety of SQLite and PostgreSQL themselves, and PostgreSQL 16 and 19, were not tested
  against the hostile statement lists. The SQLite lists were run on SQLite 3.45.3, 3.46.0 and
  3.50.4 (the version that the python.org Windows builds of Python 3.13 and 3.14 bring).
