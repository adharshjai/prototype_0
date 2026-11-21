# pcldb prototype_0

pcldb is the lab's register of instrument sessions: for every session folder on ARC
(`/projects/pcl_photonics`) it keeps one record with what was measured, when, by which
instrument, and where the files are. People find sessions in a small website, either by asking
in plain English or by writing SQL, and from a found session they go straight to its files on ARC.
This prototype runs on one computer (macOS or Windows): the database, the backend and the website
all live in this folder, and nothing here ever changes a file on ARC.

Other documents: [the HTTP API](docs/API.md), [security](docs/SECURITY.md),
[Globus Search](docs/GLOBUS_SEARCH.md), [Windows](docs/WINDOWS.md). Every setting is explained
in [pcldb_config.example.json](pcldb_config.example.json).

## The lab lead's picture, box by box

| Box in the picture | What prototype_0 has | Where it would run later |
|---|---|---|
| Instruments / simulations | Not part of pcldb. pcldb reads the upload records the Raman controller leaves behind (see "How a session gets registered"). | The instrument computers, as today. |
| Upload (Globus Transfer or SSH) | Not part of pcldb. The instrument's own uploader copies the files to ARC. | The same uploaders. |
| ARC project folder (raw files, results) | Only paths. pcldb stores where each session folder is (`/projects/pcl_photonics/...`) and never reads or writes there. | ARC storage, unchanged. |
| "After the upload succeeds: register experiment information and file paths" | `pcldb register` (four sources) and `POST /api/v1/registrations`. Today a person runs the command after an upload. | The uploader calls the registration API by itself. |
| Database backend (registration, search, permissions) | `pcldb serve`: the HTTP API and the website on `http://127.0.0.1:8787` on this computer. Users, roles and tokens. | A server that the lab's browsers can reach. Where is still open (see "Known limits"). |
| Database of shared experiment records | PostgreSQL (a database server that pcldb starts and stops inside `var/pgdata`) or SQLite (two files in `var/sqlite`). Same tables either way. | A PostgreSQL server next to the backend. |
| Team members -> website | The website in a browser on this same computer. | Every lab member's browser, with their own sign-in. |
| The teammate's "Bridge" desktop tool | The HTTP API ([docs/API.md](docs/API.md)) and the Python client `pcldb/client.py`. | The same API on the server. |
| "Open data location" -> ARC project folder | Normal view: an "Open in Globus" link. Expert view: the ARC path and ready-to-copy `scp`, `sftp`, `rsync` commands. | The same. |
| Search with the LLM (Globus Search) | A stand-in for Globus Search on this computer, and the ARC LLM when a key and the VT network are there; keyword rules when it is not. The code for the real Globus Search is written and tested against fakes only. | The hosted Globus Search index and the ARC LLM service. |

## Two lanes

The lab lead's rule: pair SSH with SQL as expert mode, and Globus with Globus Search and the
LLM as normal mode.

| | Normal (most people) | Expert (analyst and admin roles) |
|---|---|---|
| Find data | A question in plain English. The ARC LLM turns it into a Globus Search request over one entry per session, and writes a short answer. Without the LLM, keyword rules plan the search. | SQL that can only read, run on the database. |
| Get to the files | Globus: the session's folder opens in the Globus web app. | SSH: the path on ARC and `scp`, `sftp`, `rsync` commands to copy. |
| Add files | Globus Transfer. | An upload over SSH. |

Every registered session is in both lanes, whichever way it was uploaded. There is one register;
the lanes are two ways of showing it.

## Get started

### On a Mac

You need Python 3.11 or newer. `python3 --version` says which one you have. If it is older:
`brew install python@3.13`.

From a fresh clone (the repository address is the one your lab lead gives you):

```sh
git clone YOUR_REPOSITORY_ADDRESS prototype_0
cd prototype_0
python3 --version
python3 install.py
./pcldb.sh setup --engine sqlite --demo
./pcldb.sh serve --open
```

What each step does:

- `python3 install.py` makes the folder `.venv` (this folder's own Python environment) and
  installs `psycopg`, the PostgreSQL driver. It needs the internet once. `python3 install.py
  --globus` also installs `globus-sdk`, needed only for the live Globus Search.
- `./pcldb.sh setup --engine sqlite --demo` writes the settings file `pcldb_config.json`, makes
  the database, makes the user `admin` and saves its token in `var/admin_token.txt` (the token is
  never shown on the screen), and loads 16 made-up demo sessions.
- `./pcldb.sh serve --open` starts the website and opens it in your browser, already signed in.
  It prints a line like `Open http://127.0.0.1:8787/#signin=...`. That link works once, within
  five minutes. Stop the website with Ctrl+C. One project folder runs one `serve` at a time; a
  second one is refused and says which process and port has it.

`./pcldb.sh doctor` checks everything and prints one line per check (OK or PROBLEM, and the
fix). `./pcldb.sh --help` lists every command; `./pcldb.sh setup --help` explains one.

Messages, and the rest of this README, name a command with a plain `pcldb`, as in
`pcldb doctor`: type `./pcldb.sh` in its place on a Mac or Linux, and `.\pcldb.cmd` on Windows.

SQLite needs nothing installed. For PostgreSQL instead, install it once and leave out
`--engine`, in a folder that has no `pcldb_config.json` yet (a folder that was set up keeps its
engine; to change it see "To switch engines" below):

```sh
brew install postgresql@18
./pcldb.sh setup --demo
```

Without `--engine`, setup takes PostgreSQL when its programs (PostgreSQL 16 or newer) and the
`psycopg` package are found, else SQLite, and it says which it took and why. pcldb runs its own
PostgreSQL inside `var/pgdata` on `127.0.0.1`, port 15439 (`setup --port N` for another port,
below 49152). `pcldb serve` starts it when it is stopped and stops it again at the end;
`./pcldb.sh db stop` and `./pcldb.sh db start` do it by hand.

### On Windows

The short version, in PowerShell (the details, cmd, and what differs are in
[docs/WINDOWS.md](docs/WINDOWS.md)):

```powershell
winget install Python.Python.3.13
git clone YOUR_REPOSITORY_ADDRESS prototype_0
cd prototype_0
py -3 --version
py -3 install.py
.\pcldb.cmd setup --engine sqlite --demo
.\pcldb.cmd serve --open
```

For PostgreSQL: `winget install PostgreSQL.PostgreSQL.18` (it asks for administrator rights),
then `.\pcldb.cmd setup --demo`. pcldb finds the programs in `C:\Program Files\PostgreSQL\18\bin`.

### Try it without touching this folder

Every command works in another folder when the variable `PCLDB_DIR` names it (or with `--dir
FOLDER` before the command). The settings, the database and the logs then go there. This is a
good way to try things:

```sh
export PCLDB_DIR="$HOME/pcldb-try"
export PCLDB_USER_DIR="$HOME/pcldb-try/user"
./pcldb.sh setup --engine sqlite --demo
./pcldb.sh serve --port 8790
```

`PCLDB_USER_DIR` moves the personal folder (normally `~/.pcldb`, which holds your ARC LLM key
and your Globus sign-in).

## What you see in the website

A calm, light or dark page (it follows your system) with one accent colour. Longer
explanations wait behind small "Details" toggles and tooltips.

- **Sign in.** The link that `serve` prints signs you in as the local admin. Otherwise paste a
  token: the admin's is the one line of `var/admin_token.txt`; others are made with `pcldb token
  new`. The browser keeps the token for this tab only. The sign-out icon (top right) makes the
  page forget it.
- **The header**: "PCL data" on the left; the switch Normal | Expert | Status in the middle
  (Expert only for the analyst and admin roles); a "Demo data" pill when the demo is loaded,
  your initial and name, and Sign out on the right.
- **Normal.** One big search box with example chips under it (each asks a whole question) and
  an "Include simulated" switch. After a question: a one-line summary ("4 sessions" and how it
  was read), a "Details" toggle with how it was searched and the exact request, the answer
  (labelled "Written by the ARC assistant. Check it against the table." and only when the
  assistant wrote it), then the sessions: a table on a wide screen, cards on a phone. Click a
  session to open it.
- **Expert.** The query box first, with Examples, Run (Ctrl+Enter or Cmd+Enter) and Cancel
  while a query runs; the result with its row count and "Download CSV". The tables sit in a
  panel at the side (click a name to put it into the query, the arrow shows its columns).
  "History" holds your last queries, and "SSH settings" holds "Your ARC user name", which fills
  in the copy commands; it stays in this browser and is never sent to the server. A value in a
  column named `path` or `session_path` opens that session.
- **A session** opens in a panel: its key facts, the file actions of the lane you opened it
  from ("Open in Globus" and "Copy Globus address" in Normal, or "Copy ARC path" and the `scp`,
  `sftp` and `rsync` commands in Expert), its labels, and "All fields", "Runs" and "Files"
  behind toggles. A path with spaces, quotes or other unusual characters gets no commands, only
  the path.
- **Status** shows three cards (Database, Search index, Assistant), each with a coloured dot,
  one plain line and a few facts; "Details" on each card has the rest. Below them: the newest
  registrations, and "About the two modes".

## The two engines

| | PostgreSQL | SQLite |
|---|---|---|
| What to install | PostgreSQL 16 to 19 (Mac: `brew install postgresql@18`; Windows: `winget install PostgreSQL.PostgreSQL.18`) and `psycopg` (install.py) | Nothing |
| Where the data is | `var/pgdata`, a database server on `127.0.0.1:15439` that pcldb starts and stops | `var/sqlite/pcl_app.sqlite` (users, tokens, logs: private) and `var/sqlite/pcl_catalog.sqlite` (the catalogue: what expert mode reads) |
| Expert SQL runs as | the database role `pcl_reader`, which can read the catalogue only | a connection that opens only the catalogue file, read-only |
| Several people at once | yes | yes, but one writer at a time: a registration may wait up to `sql.timeout_seconds` + 5 seconds (20 s by default) behind a long expert query |

The tables, the website and the API are the same on both.

**To switch engines**, carry the records over in one file:

```sh
./pcldb.sh dump pcl_dump.jsonl
```

Then change `"engine"` in the group `"database"` of `pcldb_config.json` to `"postgresql"` or
`"sqlite"`, and:

```sh
./pcldb.sh setup
./pcldb.sh restore pcl_dump.jsonl --include-users
```

`restore` only loads into an empty database, and leaves users and tokens out unless you add
`--include-users`: then the dump's users and tokens replace the admin that `setup` made. That
`setup` wrote its own admin's token over `var/admin_token.txt`, so the old admin token is no
longer in any file: when the dump has the user `admin`, `restore` gives it a new token and saves
it in `var/admin_token.txt`, and says so. Other tokens of the dump (those under `var/tokens/`
and those handed out) work again. Without `--include-users` the admin that `setup` made stays,
with its token file. The variable `PCLDB_ENGINE` overrides the settings file. `pcldb setup`
never edits a settings file that exists; it says so when `--engine` disagrees with the file.

**One SQLite file of the catalogue, from either engine:**

```sh
./pcldb.sh export-sqlite pcl_catalog_copy.sqlite
```

It holds the sessions, files, runs, points, reviews, models and problems, and no users, tokens
or logs. It does hold operator names and notes, so keep it inside the lab's project folder. When
there is no server at all, copy it to ARC and query it there with `sqlite3` over SSH, read-only:

```sh
scp pcl_catalog_copy.sqlite YOUR_ARC_USERNAME@datatransfer.arc.vt.edu:/projects/pcl_photonics/
ssh YOUR_ARC_USERNAME@datatransfer.arc.vt.edu
sqlite3 -readonly /projects/pcl_photonics/pcl_catalog_copy.sqlite "SELECT instrument, COUNT(*) FROM sessions GROUP BY instrument"
```

The `scp` and `ssh` lines were not run from here (no ARC account in this test). Whether ARC
offers `sqlite3` on the machine you log in to was not checked; on ARC's compute machines
software usually comes through `module load`. The `sqlite3` query itself was run against an
exported file on this Mac.

`pcldb db reset --yes` empties the database of the engine in use (every record, user and token)
and makes a new admin. Without `--yes` it only explains what it would do.

## How a session gets registered after an upload

Registration writes one record per session folder: the lab card, the file list with sizes and
SHA-256 values, and where the folder is on ARC. It also queues the session's Search entry. A
folder is registered once: sending the same contents again answers `unchanged`; other contents
for the same folder are refused ("placed folders are never changed") unless an admin adds
`--replace`. Commands that write straight into the database run on the computer that holds it;
`--server` sends through a running `pcldb serve` instead (it needs a token, see below). While the
demo data is loaded, registrations are refused: remove it first with `./pcldb.sh demo clear`.

**1. The Raman controller's upload records.** After a checked upload, the Raman controller
leaves a folder `data_catalog/arc_uploads/<upload id>/` in its home folder with `record.json`,
`raman_upload_manifest.json` and `SHA256SUMS`. pcldb only reads those three files:

```sh
./pcldb.sh register --raman-uploads CONTROLLER_HOME --scan-sessions CONTROLLER_HOME/scan_sessions
```

```text
created    raman_globus_upload/simulated/1d_y_scan_20261116_225015_281  (9 file(s), 4095 bytes)
Total: 1 session(s): 1 created, 0 updated, 0 unchanged, 0 refused.
```

Only uploads in the state "verified" that are not demo uploads are taken; each other one is
listed as refused with the reason. With `--scan-sessions`, operator, location and notes come
from the session's own `session.json`, but only when it is byte for byte the file that was
uploaded. Decide once whether you use `--scan-sessions`: registering the same upload later the
other way gives different contents, and that is refused. The Raman uploader places sessions in
`raman_globus_upload/...` today, not in the `raw/<instrument>/` layout; pcldb accepts any path
below the project folder.

**2. One folder** on this computer that is a copy of what is on ARC (it must hold the lab card
`pcl_session.json` and `SHA256SUMS`). `--path` says where it is below `/projects/pcl_photonics`:

```sh
./pcldb.sh register ./1d_y_scan_20261116_225015_281 --path raw/raman-portable/simulated/1d_y_scan_20261116_225015_281
```

```text
created    raw/raman-portable/simulated/1d_y_scan_20261116_225015_281  (8 file(s), 1784 bytes)
Total: 1 session(s): 1 created, 0 updated, 0 unchanged, 0 refused.
```

No file is hashed: the SHA-256 values come from the folder's `SHA256SUMS`. `--card FILE` names
the card when it is somewhere else.

**3. A tree** laid out like ARC (`raw/<instrument>/<folder>` and
`raw/<instrument>/simulated/<folder>`); folders whose names start with `_` or `.` are skipped:

```sh
./pcldb.sh register --tree ./arc_copy
```

```text
created    raw/raman-portable/1d_y_scan_20261118_140210_455  (8 file(s), 1785 bytes)
created    raw/raman-portable/simulated/1d_x_scan_20261118_101500_120  (8 file(s), 1784 bytes)
Total: 2 session(s): 2 created, 0 updated, 0 unchanged, 0 refused.
```

**4. The nightly index.** The proof of concept's nightly build on ARC writes a `sessions.sqlite`
with sessions, analysis runs, labels, reviews and models. Copy it here and load it:

```sh
./pcldb.sh load-index sessions.sqlite
```

```text
Loaded sessions.sqlite (built 2026-11-21T01:05:58.478Z by pcldb demo 0.1.0).
Sessions: 16 (16 new, 0 changed, 0 unchanged). Files: 249.
Runs: 15. Points: 169. Reviews: 3. Models: 2 (current: demo_pca_svm_20260922). Problems: 2.
Search entries: 16 to send, 0 unchanged, 0 to remove.
```

Sessions that are in the database but not in the index are left alone and listed. Demo data and
real data never mix: `load-index` and every registration refuse while the demo is loaded
(`pcldb demo clear` first).

**5. The HTTP API, for the teammate's upload tool.** After an upload succeeds, the tool sends a
registration document (the format and examples are in [docs/API.md](docs/API.md)):

```sh
curl -s -X POST http://127.0.0.1:8787/api/v1/registrations -H "Content-Type: application/json" -H "Authorization: Bearer $(cat TOKEN_FILE)" --data-binary @body.json
```

```text
{"action":"created","path":"raw/raman-portable/simulated/2d_map_scan_20261120_120000_002","subject":"urn:pcl:dataset:...","web_url":"https://app.globus.org/file-manager?origin_id=...","n_files":5,"bytes":1303}
```

The same document can be registered from the command line with `./pcldb.sh register --document
FILE`. Any of the command-line forms goes through a running server with `--server
http://127.0.0.1:8787` and a token file:

```sh
./pcldb.sh register --tree ./arc_copy --server http://127.0.0.1:8787 --token-file TOKEN_FILE
```

The token of an uploader is made by an admin: `./pcldb.sh token new --user demo-operator-a
--label "upload laptop" --days 90` saves it in `var/tokens/` and prints only the file's name.
A token is never typed on the command line: tools read it from the variable `PCLDB_TOKEN`, else
from `--token-file`, else from `var/admin_token.txt`.

## The assistant (ARC LLM)

- It uses **your own** ARC LLM key: the key of the person running pcldb on this computer. ARC
  logs every request under the key owner's name, and a key must not be shared. A shared server
  needs ARC's answer about keys first.
- Make a key at llm.arc.vt.edu (Settings > Account > API keys) and save it as the only line of
  the file `arc_llm_api_key` in your personal folder: `~/.pcldb/arc_llm_api_key` on a Mac,
  `%USERPROFILE%\.pcldb\arc_llm_api_key` on Windows. Use a text editor, so the key does not end
  up in your shell history. On a Mac, make the file private: `chmod 600
  ~/.pcldb/arc_llm_api_key`. (Other places, in this order: the variable `PCLDB_LLM_API_KEY`, the
  file named by the setting `llm.api_key_file`, then that file.) Never put the key into
  `pcldb_config.json` or anywhere in this folder.
- A running `pcldb serve` reads the key when it starts: restart it after saving the key.
- The ARC LLM service only answers on the VT campus network or the VT VPN. Off it, the service
  answers HTTP 403, and pcldb says: connect to the VT VPN.
- Without the assistant (no key, off the VPN, or `llm.enabled` false), questions are planned by
  keyword rules on this computer, no written answer is given, and the results table is the same
  kind of real data. When a key is saved but the service cannot be reached, the answer carries a
  notice that says why. `./pcldb.sh doctor` and the Status view show the state. The keyword
  rules can exclude known values ("everything but raman", "not reviewed"); a word after "not"
  that is no known value is left out of the search, with a notice that says so.
- `./pcldb.sh ask "which sessions have adenine?"` asks from the terminal; `--no-llm` uses the
  keyword rules only.
- What the assistant sees: the question, a list of the known values of a few fields
  (instruments, methods, labels, tags, models, review states, recent sample ids), and for the
  written answer up to 20 results with a fixed set of fields. It never sees operator names,
  notes, paths, users, tokens or SQL. Details: [docs/SECURITY.md](docs/SECURITY.md).

## Globus Search

Globus Search is a hosted search index that Globus runs. It **never reads files on ARC**: it
only holds the small entries that pcldb sends it, one per session, and searches those. Every
registration queues an entry; nothing is sent until someone runs `pcldb search push`.

Today the search backend is a **stand-in on this computer** that answers the same requests over
the queued entries, with the same result shape. There is no live Globus index yet. Going live
needs the lab lead's own Globus sign-in:

```sh
python3 install.py --globus
./pcldb.sh search login
./pcldb.sh search create-index
./pcldb.sh search push
./pcldb.sh search use-globus
```

then restart `pcldb serve`. The details, what an entry holds, who may see it, the differences
between the stand-in and the real service, and the limits of a trial index are in
[docs/GLOBUS_SEARCH.md](docs/GLOBUS_SEARCH.md).

## People and roles

| Role | Ask and search | Expert SQL | Register | Manage users |
|---|---|---|---|---|
| viewer | yes | no | no | no |
| analyst | yes | yes | no | no |
| uploader | yes | no | yes | no |
| admin | yes | yes | yes | yes |

```sh
./pcldb.sh user add demo-operator-a --role uploader --display-name "Demo operator A"
./pcldb.sh user list
./pcldb.sh user role demo-operator-a --role analyst
./pcldb.sh user disable demo-operator-a
./pcldb.sh user enable demo-operator-a
./pcldb.sh token new --user demo-operator-a --days 90
./pcldb.sh token list
./pcldb.sh token revoke --id TOKEN_ID
```

`user add` without `--role` makes a viewer. User names are lower-case letters, digits and
`. _ -`. The last admin cannot be switched off or demoted. A token is `pcl_` and 43 random
characters; only its SHA-256 is stored, and the text is saved once into a private file under
`var/tokens/` (give that line to the person, then delete the file). Without `--days` a token
does not expire. The website's sign-in link gives a 12-hour token of `admin`. Today everyone
uses the website on the computer that runs `pcldb serve`; there is no per-person sign-in page.

## What is real, what is a stand-in, what was checked

Real and working on this Mac (macOS, Apple Silicon, Python 3.13.5):

- Both engines: SQLite 3.45.3, and PostgreSQL 18.7 and 17.12 (local clusters that pcldb made).
- Registration from all five sources above, the nightly index, demo data, dump, restore and
  export-sqlite, both directions between the engines.
- The expert-mode sandbox on both engines (hostile statements are refused by the database
  itself; see [docs/SECURITY.md](docs/SECURITY.md)).
- Every API endpoint, by hand against `pcldb serve` on both engines, and in the automated tests.
  The page's script runs in the tests in a scripted stand-in browser (Node) against the real
  server's answers. The website was also used in a real browser (Chromium) against `pcldb serve`
  on SQLite and on PostgreSQL 18.7: the sign-in link, the Normal lane's example questions and the
  session dialog, the Expert lane's schema list, examples and history, the SSH commands, the
  roles, and the layout at phone width in light and dark.
- Users, roles, tokens; the keyword planner for questions.

Stand-ins and gaps, said plainly:

- **Globus Search: no live index.** The stand-in on this computer answers every search. The code
  for the real service was tested only against a fake; nobody has signed in, made an index or
  sent an entry with it.
- **ARC LLM: never seen working.** The real service was reached once from this Mac, off the VT
  network, and answered HTTP 403 as expected (pcldb reported "not on the VT network or VPN").
  No successful reply has been seen, so how well the model plans questions is not known.
- **Windows: nothing here was run on a Windows PC.** The code is written for Windows (and
  Linux), and some Windows behaviour is tested with stand-ins on the Mac. The automated GitHub
  checks ([.github/workflows/ci.yml](.github/workflows/ci.yml): Windows with Python 3.11 and
  3.14, Ubuntu with Python 3.13, on every push) are the only Windows and Linux runs; the
  repository's Actions tab shows their latest result. PowerShell, cmd, Notepad, directory
  junctions and network shares were never tried by a person ([docs/WINDOWS.md](docs/WINDOWS.md)
  covers them).
- PostgreSQL 16 and 19 were not tried. Python 3.12 was tried on SQLite only (SQLite 3.50.4,
  without psycopg); Python 3.11 runs only in the GitHub checks.
- The `scp`/`sftp`/`rsync` commands and the Globus links are built from the settings; none was
  used against ARC from here.

The automated tests (`unittest`, no network) pass on this Mac on SQLite, PostgreSQL 18.7 and
17.12:

```sh
.venv/bin/python -B -m unittest discover -s tests -t .
```

A run with PostgreSQL 17 sets `PCLDB_PG_BIN=/opt/homebrew/opt/postgresql@17/bin` first. Tests
that need PostgreSQL are skipped when its programs are missing, unless `PCLDB_REQUIRE_POSTGRES=1`.

## Known limits and next steps

- **A server for the backend.** ARC documents no home for an always-on service with a database
  (its interactive apps run as time-limited jobs). The lab needs ARC's answer, or another
  machine, before the website can serve the whole lab.
- **Per-person sign-in.** Today there is one local admin link and pasted tokens. A shared server
  needs a real sign-in (for example through Globus or the university's sign-in) and HTTPS.
- **The uploader calling the registration API by itself.** Today a person runs `pcldb register`
  after an upload. The Raman controller (and the teammate's tool) should call `POST
  /api/v1/registrations` when an upload is checked.
- **Keys on a shared server.** The ARC LLM key and the Globus sign-in are personal. A shared
  server needs ARC's and Globus's answer on whose key and identity it may use.
- **Live checks still to do:** one run of the opt-in live tests on the VT VPN
  (`PCLDB_LIVE_LLM=1`), one with a trial Globus index (`PCLDB_LIVE_GLOBUS=1`), and a first run on
  a Windows PC (see [docs/WINDOWS.md](docs/WINDOWS.md)).
- **SQLite and a slow expert query.** On SQLite, a registration that arrives while an expert
  query is still reading waits for it (up to `sql.timeout_seconds` + 5 s, 20 s by default), and
  requests that come after that registration wait too. PostgreSQL has no such wait. The cure
  (SQLite's WAL mode, or a snapshot for expert SQL) is a design change left for later; see
  [docs/SECURITY.md](docs/SECURITY.md).
- Editing records in the website, per-record access rules and multi-machine use are not part of
  prototype_0.
