# pcldb on Windows

For anyone who runs pcldb on a Windows 10 or 11 PC, for example to try the teammate's upload tool
against it. Back to the [README](../README.md).

**Be aware: nothing in pcldb has been run on a Windows PC yet.** The code is written for Windows
(no shell commands, Windows paths, Windows process flags), and some of that is tested with
stand-ins on a Mac, but no real Windows run has happened. The automated GitHub checks (section 7)
are written but had not run when this was written. Please report what you see.

## 1. Install Python

pcldb needs Python 3.11 or newer. In PowerShell:

```powershell
winget install Python.Python.3.13
py -3 --version
```

Or use the installer from python.org. `py` is the Python launcher that comes with it; you do not
need to add Python to `PATH`.

## 2. Get pcldb and install its packages

```powershell
git clone YOUR_REPOSITORY_ADDRESS prototype_0
cd prototype_0
py -3 install.py
```

`install.py` makes `.venv` (this folder's own Python environment, `.venv\Scripts\python.exe`)
and installs `psycopg` (the PostgreSQL driver). It needs the internet once. `py -3 install.py
--globus` also installs `globus-sdk` for the live Globus Search.

Put the folder somewhere short, local and private: in your own user profile, for example
`%USERPROFILE%\pcldb\prototype_0` (`C:\Users\<you>\pcldb\prototype_0`), which only you and
the PC's administrators can read. Not directly under `C:\` (for example `C:\pcldb`): a folder
made there can be read by every account on the PC and changed by every signed-in account, and
Windows has no file modes that pcldb could set, so another account could read
`var\admin_token.txt` and the database passwords in `var\local_secrets.json`, or change the
program. Avoid also:

- folders synced by OneDrive, Dropbox or Google Drive. "Documents" and "Desktop" are often
  synced. A sync program locks and rewrites files while the database uses them.
- long paths. Windows programs often stop at 260 characters for a whole file path, and the
  PostgreSQL data folder adds its own subfolders.
- letters outside plain ASCII in the path.
- a network share (`\\server\share\...`): pcldb refuses to keep its SQLite files there.

## 3. (Optional) Install PostgreSQL

SQLite needs nothing, and is the easy start. For PostgreSQL:

```powershell
winget install PostgreSQL.PostgreSQL.18
```

This is the EDB installer. It asks for administrator rights. It also installs and starts its own
PostgreSQL service on port 5432; pcldb does not use that service. pcldb makes its own database
inside `var\pgdata`, on `127.0.0.1`, port 15439, and starts and stops it itself, without
administrator rights.

pcldb finds PostgreSQL's programs (it needs `initdb.exe`, `pg_ctl.exe` and `postgres.exe`, version
16 or newer) in these places: `%ProgramFiles%\PostgreSQL\<version>\bin` (where winget puts
them), `%LOCALAPPDATA%\Programs\PostgreSQL\<version>\bin`, `%PGBIN%`, `%PGROOT%\bin`,
`%USERPROFILE%\scoop\apps\postgresql\current\bin`, `%CONDA_PREFIX%\Library\bin`, and `PATH`.
Programs somewhere else (for example the EDB zip archive unpacked without administrator rights):
name the `bin` folder in the variable `PCLDB_PG_BIN` or in the setting
`database.postgresql.bin_dir`.

## 4. Run it

In **PowerShell**, from the project folder (PowerShell needs the `.\` in front):

```powershell
.\pcldb.cmd setup --engine sqlite --demo
.\pcldb.cmd doctor
.\pcldb.cmd serve --open
```

In **cmd** (Command Prompt), from the project folder:

```bat
pcldb.cmd setup --engine sqlite --demo
pcldb.cmd doctor
pcldb.cmd serve --open
```

For PostgreSQL leave out `--engine sqlite` in a folder that was not set up yet (setup takes
PostgreSQL when it finds the programs and `psycopg`, and says which it took; a folder that was
set up keeps its engine, see "To switch engines" in the README).

`pcldb.cmd` uses `.venv\Scripts\python.exe` when it is there, else `py -3`. It works from any
folder. Every command of the README works the same way: write `.\pcldb.cmd` (PowerShell) or
`pcldb.cmd` (cmd) where the README writes `./pcldb.sh`. For example:

```powershell
.\pcldb.cmd sql "SELECT instrument, COUNT(*) AS n FROM sessions GROUP BY instrument"
.\pcldb.cmd ask "which sessions have adenine?"
.\pcldb.cmd register --tree C:\data\arc_copy
.\pcldb.cmd token new --user demo-operator-a --days 90
```

A search request is easiest from a file, because quoting JSON differs between PowerShell and
cmd. In PowerShell put the `@` name in single quotes (a bare `@` is PowerShell's own splatting,
and pcldb would never see the name):

```powershell
.\pcldb.cmd search query --request '@request.json'
```

In cmd no quotes are needed: `pcldb.cmd search query --request @request.json`.

cmd replaces `%NAME%` in a command line by the variable NAME, inside quotes too, and
`pcldb.cmd` goes through cmd also when PowerShell starts it. So SQL with a LIKE pattern such as
`'%temp%'`, `'%date%'` or `'%path%'` would silently search for something else. Write such a
pattern so that no name stands between two `%`, for example `LIKE '%' || 'temp' || '%'`, or, in
PowerShell, run Python directly: `.venv\Scripts\python.exe -m pcldb sql "... LIKE '%temp%'"`.

Variables. PowerShell:

```powershell
$env:PCLDB_DIR = "$env:USERPROFILE\pcldb-try"
$env:PCLDB_USER_DIR = "$env:USERPROFILE\pcldb-try\user"
.\pcldb.cmd setup --engine sqlite --demo
```

cmd:

```bat
set PCLDB_DIR=%USERPROFILE%\pcldb-try
set PCLDB_USER_DIR=%USERPROFILE%\pcldb-try\user
pcldb.cmd setup --engine sqlite --demo
```

These last until the window is closed.

## 5. Where files live

| What | Where |
|---|---|
| The program | the project folder (`pcldb\`, `pcldb.cmd`, `install.py`) |
| Its Python environment | `.venv\` in the project folder |
| Settings | `pcldb_config.json` in the project folder (Notepad may save it with a byte order mark; pcldb accepts that) |
| Databases, logs, local passwords, the admin token | `var\` in the project folder: `var\sqlite\`, `var\pgdata\`, `var\log\`, `var\local_secrets.json`, `var\admin_token.txt`, `var\tokens\` |
| Your ARC LLM key and Globus sign-in | `%USERPROFILE%\.pcldb\arc_llm_api_key` and `%USERPROFILE%\.pcldb\globus_tokens.json` |

With `PCLDB_DIR` set, the settings file and `var\` are in that folder instead.

Save the ARC LLM key as the only line of `%USERPROFILE%\.pcldb\arc_llm_api_key` (make the
folder `.pcldb` first). The file name has no `.txt`: in Notepad's "Save as", choose "All files
(\*.\*)" as the type, or type the name in quotes (`"arc_llm_api_key"`); otherwise Notepad saves
`arc_llm_api_key.txt` and pcldb does not find the key (`.\pcldb.cmd doctor` then says so).
Do not type the key into a command: it would stay in the command history. On a normal Windows setup, the folders in your user profile
can be read only by you and the PC's administrators.

## 6. What is different on Windows

- **Ctrl+C.** It stops `serve` (and the PostgreSQL database that `serve` started). Started
  through `pcldb.cmd`, cmd then asks "Terminate batch job (Y/N)?"; pcldb has already stopped by
  then, so either answer is fine. To avoid the question, run Python directly from the project
  folder: `.venv\Scripts\python.exe -m pcldb serve --open`. pcldb starts PostgreSQL in its own
  process group, so Ctrl+C in the window of `serve` does not hit the database directly; `serve`
  stops it in order.
- **The firewall.** The website and pcldb's PostgreSQL listen only on `127.0.0.1`, this
  computer's own address. If Windows asks whether to allow Python or `postgres` to communicate
  on networks, you may refuse: nothing from outside is needed. Whether the question appears at
  all for such a program is not documented by Microsoft and was not seen here.
- **Folder rights for PostgreSQL.** PostgreSQL's programs run with reduced rights on Windows:
  started by an administrator, they drop the administrator rights. They cannot use a folder that
  only administrators may open, and `setup` then stops with initdb's "Permission denied" (pcldb
  adds a sentence that says so). A folder in your own user profile (section 2) works. Such a
  folder comes, for example, from Python 3.13 or newer run as administrator: there
  `tempfile.mkdtemp` gives a new folder rights for SYSTEM, Administrators and its owner only.
  pcldb's tests and `tools/smoke.py` therefore make their temporary folders without it.
- **Antivirus.** The first `setup` with PostgreSQL makes about a thousand files and the first
  start can be slow while they are scanned; pcldb waits up to 120 seconds for PostgreSQL to
  start on Windows. When a scanner holds a file that pcldb wants to replace, pcldb tries again
  for about a second and then says the file is in use; run the command again. If it keeps
  happening, ask whoever looks after the PC whether `var\pgdata` may be left out of scanning.
- **curl.** In Windows PowerShell, `curl` is another command. Write `curl.exe` for the examples in
  [API.md](API.md), quote `'@body.json'`, and read a token file with `$(Get-Content FILE)`;
  API.md shows the PowerShell line.
- **Ports.** Keep the PostgreSQL port below 49152 (`setup --port N`): Windows hands out the ports
  above it to other programs. The website's port is 8787 (`serve --port N` for another).
- **Line endings.** `pcldb.cmd` must keep Windows line endings (CRLF), every other file has Unix
  ones (LF). The file `.gitattributes` tells Git not to change line endings in either direction,
  whatever your `core.autocrlf` setting says, so test files keep the hashes the tests expect.
- **Console text.** pcldb prints plain ASCII, so any console shows it. Characters from records
  that are not ASCII are written as escapes like `\xe9`.

## 7. What was checked and what was not

Checked on the Mac (macOS) only, with stand-ins for Windows:

- that `pcldb.cmd` has CRLF line endings and chooses `.venv\Scripts\python.exe` or `py -3`;
- that PostgreSQL's programs are looked for in the Windows places above;
- that the Windows process flags for starting PostgreSQL (no window, its own process group) and
  the longer start time are passed;
- that writing a file retries when Windows says the file is in use;
- that `install.py` takes the right steps for Windows (recorded, not run).

Written to run on GitHub's Windows machines on every push
([.github/workflows/ci.yml](../.github/workflows/ci.yml), Windows with Python 3.11 and 3.14,
PostgreSQL 17 from the machine image), but **not run yet** when this was written, because the
repository was not on GitHub yet. When it runs, it checks:

- the whole test suite, with PostgreSQL required (`PCLDB_REQUIRE_POSTGRES=1`);
- the PostgreSQL tests again with wrong `PG*` variables set, which pcldb must ignore;
- `tools/smoke.py --engine both`: setup with demo data, doctor, sql, ask, serve started and
  stopped the way Ctrl+C does, on both engines in a temporary folder with a space in its name,
  and `pcldb.cmd` itself from a copy of the code;
- SQLite on a bare Python without `psycopg` and `globus-sdk`.

Look at the Actions tab of the repository on GitHub for the result.

Not checked by anyone, and not by those automated runs either:

- `install.py` on a Windows PC (the automated run installs the packages with pip directly);
- a person's Ctrl+C in a real console window, and cmd's "Terminate batch job" question;
- the firewall question, antivirus delays, OneDrive folders, long paths;
- PostgreSQL installed with winget (the automated run uses the image's PostgreSQL 17);
- the website in Edge on Windows.

A first run on a Windows PC: follow sections 1, 2 and 4 with SQLite, then
`.venv\Scripts\python.exe -B -m unittest discover -s tests -t .` from the project folder (the
tests need no network), then the same with PostgreSQL installed.
