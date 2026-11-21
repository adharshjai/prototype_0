# The HTTP API

`pcldb serve` runs the website and this API on one address, by default
`http://127.0.0.1:8787`. Every endpoint is under `/api/v1`. This page is for anyone who writes
a tool against it, for example an upload tool that registers a session after the upload
succeeded. Back to the [README](../README.md).

All data on this page is made up. Long values are cut and shown as `...`.

## The rules for every request

- **JSON only.** A POST must send `Content-Type: application/json` and a `Content-Length`
  header (no chunked bodies). The body is a JSON object in UTF-8. Answers are JSON with every
  character outside ASCII written as `\u....`.
- **Sign in with a token** in the header `Authorization: Bearer <token>`. Every endpoint needs
  one except `GET /api/v1/health`, `POST /api/v1/signin` and the website's own files. A token is
  never accepted in the address: a query parameter named `token`, `access_token`, `auth`,
  `authorization`, `api_key`, `apikey`, `key`, `password` or `bearer` is refused.
- **Where tokens come from.** An admin runs `pcldb token new --user demo-operator-a --days 90`;
  the token is saved in a private file under `var/tokens/` and only the file name is printed.
  The website gets a 12-hour token of the user `admin` through `POST /api/v1/signin`.
- **The Host header** must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`, or a
  name listed in the setting `server.allowed_hosts`. Anything else gets 421. This stops other web
  pages from reaching the server under a made-up name.
- **Body limits:** 64 KB, and 16 MB for `POST /api/v1/registrations`.
- **Headers on every answer:** `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store` and the content security policy given in
  [SECURITY.md](SECURITY.md). There are no CORS headers: other web sites cannot call the API
  from a browser.
- **One request per connection** (HTTP/1.0). A client that stops sending is dropped after 30
  seconds. The request line and headers must be in within 30 seconds. A body may come slowly
  but must keep coming: at least 16 KB in every 30 seconds, and all of it at 16 KB per second
  on average (16 MB in about 17 minutes). A request that is cut off gets 408 `timeout`.
  `pcldb.client` sends a body in pieces and looks for an answer before each piece, so it gets
  that 408 even while it is still sending. A client that sends the whole body before it reads
  (urllib) may only see the closed connection.
- **A refusal reaches a client that sends the whole body first.** A 401 or 403 is decided
  before the body is read; the server then reads the rest of the body (within the limits
  above) and throws it away, so urllib and `pcldb.client` get the 401 or 403, not a broken
  connection.
- **Times** are UTC text with milliseconds, like `2026-11-20T14:30:05.120Z`, except inside
  search entries, which carry seconds (`2026-11-20T14:30:05Z`).

## Errors

Every error has the same shape and an HTTP status:

```json
{"error": {"code": "already_registered",
           "message": "\"raw/raman-portable/1d_y_scan_20261120_143005_120\" is already registered with other contents; placed folders are never changed. Nothing was registered. If the first registration was wrong, an admin can register it again with --replace.",
           "details": null}}
```

`message` is a sentence for a person. `code` is for programs. `details` is an object or null.

| Status | When | Codes |
|---|---|---|
| 400 | The request is wrong: not JSON, an unknown key, a refused document, a refused search request, refused SQL, a token in the address | `bad_json`, `bad_request`, `bad_question`, `bad_search_request`, `sql_refused`, `sql_error`, `sql_timeout`, `token_in_address`, `invalid_document`, `invalid_card`, `bad_path`, `bad_number`, `unknown_schema`, `instrument_mismatch`, `simulated_mismatch`, `name_mismatch`, `too_many_files` |
| 401 | No token, a wrong token (unknown, revoked, expired, or its user is switched off), or a sign-in code that is used or old. Comes with `WWW-Authenticate: Bearer realm="pcldb"`. | `unauthorized`, `signin_refused` |
| 403 | The user's role does not allow it | `forbidden` |
| 404 | No such address, or no such session | `not_found`, `no_such_session` |
| 405 | Wrong method for a known address (the `Allow` header names the right one) | `method_not_allowed` |
| 408 | The request was not sent in time | `timeout` |
| 409 | The session is registered already with other contents, or under a spelling that differs only in letter case; or the demo data is loaded (remove it first: `pcldb demo clear`); or the upload id is registered already at another path (the folder was moved by hand) | `already_registered`, `case_conflict`, `demo_loaded`, `upload_moved` |
| 411 | No `Content-Length` | `length_required` |
| 413 | The body is too large | `too_large` |
| 414 | The address is too long (more than 8192 characters) | `too_long` |
| 415 | The body is not sent as `application/json` | `wrong_content_type` |
| 421 | The Host header names something else | `wrong_host` |
| 429 | Too many questions from one person in a minute (setting `ask.per_user_per_minute`, 20) | `ask_rate_limited` (`details.retry_after_s` says how long to wait) |
| 500 | A fault inside pcldb. The message is generic; the details go only to `var/log/server.log`. | `internal_error` |
| 503 | The database is busy or down, too many expert queries at once, or the expert-mode role is unsafe | `db_busy`, `db_down`, `not_initialized`, `sql_busy`, `sandbox_unsafe` |

The table lists the codes that are seen most. Other modules can add codes of their own; a
program should act on the status first and show `message` to the person.

## Who may call what

| Endpoint | Needs |
|---|---|
| `GET /health`, `POST /signin` | nothing |
| `GET /me`, `GET /status` | any valid token |
| `POST /ask`, `POST /search`, `GET /session`, `GET /registrations` | the capability `ask` (every role) |
| `POST /sql`, `GET /sql/schema`, `GET /sql/examples`, `GET /sql/history` | `sql` (analyst, admin) |
| `POST /registrations` | `register` (uploader, admin); with `"replace": true` also `manage` (admin) |

## The endpoints

### GET /api/v1/health

No token. Says that the server runs.

```json
{"ok": true, "version": "0.1.0"}
```

### POST /api/v1/signin

No token. Swaps the one-time code from the address that `pcldb serve` prints
(`http://127.0.0.1:8787/#signin=<code>`) for a 12-hour token of the local admin. The code is 32
characters, works once, and only within five minutes of the start of `pcldb serve`.

Request:

```json
{"code": "THE_CODE_FROM_THE_ADDRESS"}
```

Answer:

```json
{"token": "pcl_...",
 "expires_utc": "2026-11-21T02:30:00.000Z",
 "user": {"name": "admin", "display_name": "Local admin", "role": "admin"}}
```

A code that was used or is too old: 401 `signin_refused`.

### GET /api/v1/me

Who you are and what you may do.

```json
{"user": {"name": "demo-reviewer", "display_name": null, "role": "analyst"},
 "capabilities": ["ask", "sql"],
 "engine": {"name": "sqlite", "version": "3.45.3"},
 "defaults": {"include_simulated": true, "sql_max_rows": 1000},
 "storage": {"ssh_host": "datatransfer.arc.vt.edu",
             "project_path": "/projects/pcl_photonics",
             "collection_id": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"},
 "version": "0.1.0"}
```

### GET /api/v1/status

The state of the database, the search queue and the assistant, and the newest 10 registrations.

```json
{"database": {"engine": "postgresql", "version": "18.7", "where": "this computer, port 15439",
              "sessions": 16, "files": 249, "runs": 15, "points": 169, "demo": true,
              "instruments": [{"instrument": "raman-portable", "sessions": 12},
                              {"instrument": "jeol-jsm-it800-shl", "sessions": 4}],
              "newest_started_utc": "2026-11-20T10:17:58.253Z"},
 "search": {"backend": "local", "index_set": false, "signed_in": false,
            "entries": {"pending": 16, "sent": 0, "synced": 0, "failed": 0, "delete_pending": 0}},
 "assistant": {"enabled": true, "key_present": true, "model": "gpt-oss-120b",
               "state": "off_vpn",
               "note": "This computer is not on the VT campus network or VPN, and the ARC LLM service only answers there. Connect to the VT VPN to use the assistant; until then questions are answered by the keyword rules."},
 "registrations": [{"at_utc": "2026-11-20T14:42:01.317Z", "user_name": "demo-operator-a",
                    "session_path": "raw/raman-portable/1d_y_scan_20261120_143005_120",
                    "action": "created", "source": "upload"}]}
```

- `database.where` says where the data is (a folder for SQLite, a port for PostgreSQL).
- `assistant.state` is one of `ready`, `off_vpn`, `bad_key`, `no_key`, `disabled`, `error`. It is
  checked with one small request to the ARC LLM service and kept for 60 seconds.
- `search.signed_in` only looks for a saved Globus sign-in on this computer; it uses no network.
- `database.expert_note` appears only on an external PostgreSQL where pcldb could not apply every
  limit to the expert-mode role ([SECURITY.md](SECURITY.md)).

### POST /api/v1/ask

A question in plain English (normal mode). `include_simulated` may be left out; then the setting
`ask.include_simulated_default` decides.

Request:

```json
{"question": "adenine sessions from this month", "include_simulated": true}
```

Answer (one result shown):

```json
{"question": "adenine sessions from this month",
 "answer": null, "answer_by": "none", "planned_by": "rules",
 "interpretation": "Sessions where labels has adenine, started from 2026-11-01 to 2026-11-30, newest first (up to 10).",
 "request": {"@version": "query#1.0.0", "q": "*", "limit": 10, "offset": 0,
             "filters": [{"type": "match_any", "field_name": "labels", "values": ["adenine"]},
                         {"type": "range", "field_name": "started",
                          "values": [{"from": "2026-11-01T04:00:00Z", "to": "2026-12-01T04:59:59Z"}]},
                         {"type": "match_any", "field_name": "withdrawn", "values": [false]},
                         {"type": "match_any", "field_name": "is_copy", "values": [false]}],
             "sort": [{"field_name": "started", "order": "desc"}]},
 "backend": "local",
 "notices": ["The ARC LLM service only answers on the VT campus network or VPN. Connect to the VT VPN and ask again. This question was answered by the keyword rules instead."],
 "total": 4, "count": 4,
 "results": [{"subject": "urn:pcl:dataset:...",
              "title": "1d_y_scan_20261119_150917_113",
              "folder": "1d_y_scan_20261119_150917_113",
              "path": "raw/raman-portable/simulated/1d_y_scan_20261119_150917_113",
              "instrument": "raman-portable", "method": "1d-y", "completion": "complete",
              "sample_ids": ["DEMO-S-102"], "tags": ["demo"],
              "labels": ["UNKNOWN", "adenine"],
              "label_counts": [{"count": 3, "label": "UNKNOWN"}, {"count": 10, "label": "adenine"}],
              "review_state": "pending", "n_needs_review": 1,
              "simulated": true, "is_copy": false, "withdrawn": false, "corrected": false,
              "items": 13, "items_planned": 13, "files": 17, "bytes": 576619,
              "started": "2026-11-19T15:09:17Z", "uploaded": "2026-11-19T15:57:17Z",
              "web_url": "https://app.globus.org/file-manager?origin_id=8e804f3a-5bac-4b6d-9836-bcd5b9c133ec&origin_path=...",
              "globus_url": "globus://8e804f3a-5bac-4b6d-9836-bcd5b9c133ec/pcl_photonics/raw/...",
              "...": "..."}],
 "facets": [],
 "timing_ms": {"plan": 129, "search": 5, "answer": 0}}
```

- `answer` is text written by the ARC assistant (`answer_by` `llm`), a fixed sentence when
  nothing matched, or null.
- `planned_by` is `llm` (the assistant planned the search) or `rules` (the keyword rules).
- `interpretation` is made by pcldb from the request that ran, never by the assistant.
- The server always adds `withdrawn = false` and `is_copy = false`, and `simulated = false` when
  `include_simulated` is false.
- Each result is the session's search entry ([GLOBUS_SEARCH.md](GLOBUS_SEARCH.md)) plus
  `subject`. Empty fields are left out.
- `facets` holds counts when the question asked for them ("how many sessions per instrument"):
  `[{"name": "instrument", "field_name": "instrument", "buckets": [{"value": "raman-portable", "count": 12}]}]`.
- Errors: 400 `bad_question` (empty, or over 500 characters), 429 `ask_rate_limited`.

### POST /api/v1/search

One search request in the Globus Search language (the subset pcldb accepts is in
[GLOBUS_SEARCH.md](GLOBUS_SEARCH.md)). The answer has the shape the Globus Search service
gives, whichever backend answered.

Request:

```json
{"request": {"q": "*", "limit": 1,
             "filters": [{"type": "match_any", "field_name": "instrument", "values": ["raman-portable"]}],
             "facets": [{"name": "labels", "type": "terms", "field_name": "labels", "size": 5}]}}
```

Answer:

```json
{"result": {"@datatype": "GSearchResult", "@version": "2017-09-01",
            "total": 12, "count": 1, "offset": 0, "has_next_page": true,
            "gmeta": [{"@datatype": "GMetaResult", "@version": "2017-09-01",
                       "subject": "urn:pcl:dataset:...",
                       "entries": [{"entry_id": null, "matched_principal_sets": [],
                                    "content": {"folder": "1d_y_scan_20261120_101758_253", "...": "..."}}]}],
            "facet_results": [{"@datatype": "GFacetResult", "@version": "2017-09-01", "name": "labels",
                               "buckets": [{"@datatype": "GBucket", "@version": "2017-09-01",
                                            "value": "UNKNOWN", "count": 9}]}]},
 "backend": "local",
 "notices": []}
```

`backend` is `local` (the stand-in on this computer) or `globus`. When Globus Search was asked
but could not answer, `notices` says so and the stand-in answered.

A field that does not exist, or a value of the wrong type, is refused (400
`bad_search_request`); `details.where` points at the place:

```json
{"error": {"code": "bad_search_request",
           "message": "filters[0].field_name names the field \"colour\", which is not a field of the session entries. The fields are: bytes, collection_id, ...",
           "details": {"where": "filters[0].field_name"}}}
```

### GET /api/v1/session?path=...

One session with its files (the first 500 and the total), analysis runs and label counts. The
path is the one in the results, percent-encoded:
`/api/v1/session?path=raw%2Framan-portable%2F1d_y_scan_20261120_143005_120`.

```json
{"session": {"path": "raw/raman-portable/1d_y_scan_20261120_143005_120",
             "instrument": "raman-portable", "area": "real",
             "folder": "1d_y_scan_20261120_143005_120",
             "session_id": "demo-session-0042", "is_copy": false,
             "started_utc": "2026-11-20T14:30:05.120Z", "simulated": false,
             "sample_ids": ["DEMO-S-301"], "operator": "demo-operator-a",
             "completion": "complete", "n_files": 4, "bytes": 7314,
             "upload_json": {"transport": "ssh", "upload_id": "u00000000002a", "checked": true,
                             "check_method": "sha256-on-arc", "checked_utc": "2026-11-20T14:41:00.000Z"},
             "collection_id": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
             "data_path": "/pcl_photonics/raw/raman-portable/1d_y_scan_20261120_143005_120",
             "storage_path": "/projects/pcl_photonics/raw/raman-portable/1d_y_scan_20261120_143005_120",
             "globus_url": "globus://8e804f3a-5bac-4b6d-9836-bcd5b9c133ec/pcl_photonics/raw/raman-portable/1d_y_scan_20261120_143005_120/",
             "web_url": "https://app.globus.org/file-manager?origin_id=...&origin_path=...",
             "subject": "urn:pcl:dataset:...",
             "registered_utc": "2026-11-20T14:42:01.317Z", "registered_by": "demo-operator-a",
             "source": "upload", "...": "..."},
 "files": {"total": 4, "shown": 4,
           "items": [{"rel_path": "SHA256SUMS", "bytes": 260, "sha256": "4444...4444", "listed": true}]},
 "runs": [{"path": "results/...", "folder": "...", "kind": "classify",
           "made_utc": "2026-11-20T15:00:00.000Z", "model_name": "demo_pca_svm_20260922",
           "run_by": "demo-classifier", "valid": true}],
 "labels": [{"label": "UNKNOWN", "count": 3}, {"label": "adenine", "count": 10}],
 "review_state": "pending", "n_needs_review": 1}
```

`session` holds every column of the table `sessions` (the expert view's schema browser describes
each). A path that is not registered: 404 `no_such_session`.

### POST /api/v1/sql

One read-only SQL statement (expert mode). `max_rows` may be left out; it is never more than the
setting `sql.max_rows` (1000).

Request:

```json
{"sql": "SELECT instrument, COUNT(*) AS n FROM sessions GROUP BY instrument", "max_rows": 1000}
```

Answer:

```json
{"columns": [{"name": "instrument", "type": "text"}, {"name": "n", "type": "integer"}],
 "rows": [["jeol-jsm-it800-shl", 4], ["raman-portable", 12]],
 "row_count": 2, "truncated": false, "elapsed_ms": 2}
```

`type` is the engine's own type name (so it differs between SQLite and PostgreSQL). `truncated`
is true when there were more rows than `max_rows`, or more than 32 MB of values. A long text cell
is cut at `sql.max_cell_chars` (10000) characters with a mark.

Errors (all 400 unless said):

```json
{"error": {"code": "sql_refused",
           "message": "Expert mode only reads the catalogue. A statement must start with SELECT, WITH, VALUES or EXPLAIN (this one starts with DELETE).",
           "details": {"first_word": "DELETE", "allowed": ["SELECT", "WITH", "VALUES", "EXPLAIN"]}}}
```

```json
{"error": {"code": "sql_error", "message": "SQLite said: no such table: users", "details": null}}
```

- `sql_refused`: the statement is not a read, or the database refused it as not allowed (on
  PostgreSQL also `TABLE name` is accepted as a first word).
- `sql_error`: the database's own message (PostgreSQL adds `details.line`, `details.column`,
  `details.sqlstate` when it gives them).
- `sql_timeout`: the statement ran longer than `sql.timeout_seconds` (15).
- 503 `sql_busy`: more than 4 expert queries at once.
- 503 `sandbox_unsafe`: on an external PostgreSQL whose expert-mode role could read private data.

### GET /api/v1/sql/schema

The tables and views expert mode can read, with columns, types and descriptions.

```json
{"engine": "sqlite", "schema": "pcl",
 "objects": [{"name": "sessions", "kind": "table",
              "description": "One row per session folder on ARC: what was measured, when, with which instrument, and where the files are.",
              "columns": [{"name": "path", "type": "TEXT", "nullable": false,
                           "description": "The session folder's path below the project folder ..."}]}]}
```

### GET /api/v1/sql/examples

Example queries for the engine in use (16 each).

```json
{"engine": "sqlite",
 "examples": [{"title": "Newest sessions",
               "sql": "-- The 20 newest sessions. Withdrawn sessions are left out.\nSELECT folder, instrument, started_utc, method, completion, simulated\nFROM sessions\n..."}]}
```

### GET /api/v1/sql/history

Your own last 50 SQL queries, newest first, refused ones included.

```json
{"items": [{"at_utc": "2026-11-20T14:50:34.032Z", "query_text": "SELECT * FROM users",
            "ok": false, "n_rows": null, "elapsed_ms": 0,
            "error_text": "sql_error: SQLite said: no such table: users"}]}
```

### POST /api/v1/registrations

Register one session after its upload succeeded. Needs the role uploader or admin.
`"replace": true` (admin only) replaces a session that is registered with other contents.

Request: `{"document": <a registration document>, "replace": false}`. The document:

```json
{
  "schema": "pcl-registration/1",
  "location": {"path": "raw/raman-portable/1d_y_scan_20261120_143005_120",
               "collection_id": null, "project_path": null},
  "card": {
    "schema": "pcl-session/1",
    "instrument": "raman-portable",
    "session": "1d_y_scan_20261120_143005_120",
    "session_id": "demo-session-0042",
    "started_utc": "2026-11-20T14:30:05.120Z",
    "ended_utc": "2026-11-20T14:31:40.002Z",
    "simulated": false,
    "sample_ids": ["DEMO-S-301"],
    "operator": "demo-operator-a",
    "method": "1d-y",
    "items": 13,
    "items_planned": 13,
    "completion": "complete",
    "format": "raman-session/1 folder",
    "uploaded_utc": "2026-11-20T14:40:12.500Z",
    "upload_id": "u00000000002a",
    "written_by": {"name": "demo-uploader", "version": "1.0"},
    "tags": ["demo"]
  },
  "files": [
    {"rel_path": "session.json", "bytes": 1234,
     "sha256": "1111111111111111111111111111111111111111111111111111111111111111"},
    {"rel_path": "point_0001.json", "bytes": 5120,
     "sha256": "2222222222222222222222222222222222222222222222222222222222222222"},
    {"rel_path": "pcl_session.json", "bytes": 700,
     "sha256": "3333333333333333333333333333333333333333333333333333333333333333"},
    {"rel_path": "SHA256SUMS", "bytes": 260,
     "sha256": "4444444444444444444444444444444444444444444444444444444444444444"}
  ],
  "sums_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
  "upload": {"transport": "ssh", "upload_id": "u00000000002a", "checked": true,
             "check_method": "sha256-on-arc", "checked_utc": "2026-11-20T14:41:00.000Z"}
}
```

Answer:

```json
{"action": "created", "path": "raw/raman-portable/1d_y_scan_20261120_143005_120",
 "subject": "urn:pcl:dataset:...",
 "web_url": "https://app.globus.org/file-manager?origin_id=8e804f3a-5bac-4b6d-9836-bcd5b9c133ec&origin_path=%2Fpcl_photonics%2Fraw%2Framan-portable%2F1d_y_scan_20261120_143005_120%2F",
 "n_files": 4, "bytes": 7314}
```

`action` is `created`, `updated` (with `replace`) or `unchanged` (the same document again:
nothing is written but the audit row). Every registration through the API is recorded with the
source `upload` and the caller's user name.

The rules of the document, in short (each refusal is a 400 or 409 with a sentence that says
what to fix; nothing is written):

- `location.path` is the folder's place below the project folder `/projects/pcl_photonics`: a
  relative path with `/`, 1 to 1024 characters, no empty part, no `.` or `..`, no backslash, no
  part that Windows could not hold (a trailing dot or space, `CON`, `NUL`, `COM1` ...). When it
  has the shape `raw/<instrument>/<folder>` or `raw/<instrument>/simulated/<folder>`, the
  instrument must match the card, `simulated/` must match `card.simulated`, and the folder must
  be `card.session` (or `<session>_copy<N>`). Any other path below the project folder is
  accepted as it is. Paths are case-sensitive and stored exactly as sent.
- `location.collection_id` and `location.project_path` may be null: the settings give them.
- The card follows `pcldb/schemas/pcl_session.schema.json`. Required: `schema`, `instrument`,
  `session`, `session_id`, `started_utc`, `simulated`, `sample_ids`, `completion` (`complete`,
  `partial`, `failed` or `unknown`), `format`, `uploaded_utc`, `written_by`. Times are UTC with
  `Z`. No other keys than those in the schema file.
- `files`: every file of the folder with `rel_path`, `bytes` and `sha256` (64 lower-case hex
  digits, or null when the folder's checksum list does not name it); at most 100000. The entry
  for `SHA256SUMS` carries that file's own SHA-256, the same value as `sums_sha256`.
- `upload` may be left out. When it is there, only these five keys are accepted: `transport`
  (`globus`, `ssh` or `local`), `upload_id`, `checked`, `check_method` (`sha256-on-arc`,
  `globus-checksum`, `globus-verify` or `none`), `checked_utc`. No account names.
- The same folder with other contents: 409 `already_registered`. The same folder spelled with
  other letter case: 409 `case_conflict`. While the demo data is loaded: 409 `demo_loaded`
  (demo data and real data never mix; `pcldb demo clear` removes the demo). An upload id that
  is registered already at another path: 409 `upload_moved`.

### GET /api/v1/registrations

The newest 50 audit rows: every registration, including refused ones.

```json
{"items": [{"at_utc": "2026-11-20T14:42:01.317Z", "user_name": "demo-operator-a",
            "session_path": "raw/raman-portable/1d_y_scan_20261120_143005_120",
            "action": "created", "source": "upload"}]}
```

`action` is `created`, `updated`, `unchanged` or `rejected`; `source` is `upload`, `tree`,
`index` or `demo`.

### The website's files

`GET /` (the same as `/index.html`), `/app.js`, `/app.css`, `/favicon.svg`. Nothing else is
served; no file name is ever taken from the address.

## Calling it from a tool

With curl (on Windows write `curl.exe`, because in Windows PowerShell `curl` names another
command):

```sh
curl -s http://127.0.0.1:8787/api/v1/health
curl -s -X POST http://127.0.0.1:8787/api/v1/registrations -H "Content-Type: application/json" -H "Authorization: Bearer $(cat TOKEN_FILE)" --data-binary @body.json
```

`body.json` holds `{"document": {...}, "replace": false}`.

The same in Windows PowerShell, where a bare `@` starts PowerShell's own splatting and `cat`
works differently (the quotes around `'@body.json'` matter):

```powershell
curl.exe -s -X POST http://127.0.0.1:8787/api/v1/registrations -H "Content-Type: application/json" -H "Authorization: Bearer $(Get-Content TOKEN_FILE)" --data-binary '@body.json'
```

In cmd: `set /p TOKEN=<TOKEN_FILE`, then the curl line with `-H "Authorization: Bearer %TOKEN%"`
and `--data-binary @body.json`.

With plain Python (standard library only, any system). Save the document as `registration.json`
and the token as the one line of `TOKEN_FILE`:

```python
import json
import urllib.error
import urllib.request

SERVER = "http://127.0.0.1:8787"          # the address pcldb serve prints
TOKEN_FILE = "TOKEN_FILE"                  # a file whose one line is the uploader's token

with open(TOKEN_FILE, encoding="utf-8") as handle:
    token = handle.readline().strip()
with open("registration.json", encoding="utf-8") as handle:
    document = json.load(handle)

request = urllib.request.Request(
    SERVER + "/api/v1/registrations",
    data=json.dumps({"document": document}).encode("utf-8"),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    method="POST")
try:
    with urllib.request.urlopen(request, timeout=60) as answer:
        print(json.load(answer))
except urllib.error.HTTPError as error:
    print(error.code, json.load(error)["error"]["message"])
```

The first run prints `{'action': 'created', ...}`, a second run `{'action': 'unchanged', ...}`.

With pcldb's own client (when the `pcldb` folder is importable):

```python
from pcldb.client import Client

client = Client("http://127.0.0.1:8787", token)
result = client.register(document)                 # after an upload succeeded
answer = client.ask("adenine sessions from last week")
rows = client.sql("SELECT COUNT(*) AS n FROM sessions")
```

It raises `pcldb.errors.PclError` with the server's `code`, `message` and `status`; a server
that cannot be reached is `Unavailable` (`server_unreachable`), and one that closed the
connection while the body was still being sent without an answer that could be read is
`Unavailable` (`server_closed`; the server's log says why). It follows no redirects and uses no proxy, so the token
only goes to the address you gave. `pcldb register ... --server URL` uses the same client.

Never put the token into the address, a log, or the tool's source code: read it from a file or
the variable `PCLDB_TOKEN`.
