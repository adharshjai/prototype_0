"""The project-local PostgreSQL cluster: find the programs, create, start, stop (section 5).

pcldb never needs a PostgreSQL service or administrator rights. It keeps its own cluster in
`var/pgdata`, listening on 127.0.0.1 only (TCP; no socket files), with passwords it generates
into `var/local_secrets.json`. The server is always driven through `pg_ctl`.

Rules that come from the research report (research_postgres_local.md):

- Every child process gets a clean environment: the PG* variables are removed, and on macOS
  `LC_ALL` is set when the caller has no locale at all (without one the server stops with
  "postmaster became multithreaded during startup").
- `pg_ctl start` never gets a pipe: its output goes to a file in `var/log/`. A pipe would be
  inherited by the server, and on Windows the call would never return.
- Every setting goes into `postgresql.conf` (a block that pcldb rewrites), never through
  `pg_ctl -o`, whose quoting differs per system.
- The locale is the same on every system: encoding UTF8, libc locale C, and on PostgreSQL 17 and
  newer the built-in provider with C.UTF-8. PostgreSQL 16 has no built-in provider and uses
  plain C rules. Text sorts by code point either way, as SQLite sorts it.
- A left-over `postmaster.pid` is the server's business. Only when a start fails, nothing
  answers on the port and the recorded process is not PostgreSQL is the file removed and the
  start tried once more.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import util
from ..errors import PclError

MIN_MAJOR = 16
PROGRAMS = ("initdb", "pg_ctl", "postgres")
OWNER_ROLE = "pcl_owner"
SECRET_ROLES = ("owner", "app", "reader")

_NOT_FOUND = "PostgreSQL's programs were not found. "
_INSTALL = ("Install PostgreSQL (Mac: brew install postgresql@18; Windows: winget install "
            "PostgreSQL.PostgreSQL.18)")


def _what_to_do(settings):
    """The way out: a new project may still choose SQLite; one that was set up with PostgreSQL
    keeps it (its records are in it), so there only installing it or naming its folder helps."""
    config_path = getattr(settings, "config_path", None)
    if config_path is not None and config_path.exists():
        return ("%s again, or name the folder of its programs in database.postgresql.bin_dir "
                "(or the variable PCLDB_PG_BIN). This project was set up with PostgreSQL, and its "
                "records are in it." % _INSTALL)
    return "%s or run: pcldb setup --engine sqlite" % _INSTALL


_CONF_BEGIN = "# --- pcldb settings: pcldb rewrites this block; change pcldb_config.json instead ---"
_CONF_END = "# --- end of pcldb settings ---"
_CONF_BLOCK = re.compile(r"\n*" + re.escape(_CONF_BEGIN) + r".*?" + re.escape(_CONF_END) + r"\n?",
                         re.DOTALL)
_VERSION_TEXT = re.compile(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?")


def current_system():
    """"windows", "darwin" or "linux" (every other Unix counts as linux)."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------------- finding the programs


@dataclass(frozen=True)
class PgBin:
    """One folder with PostgreSQL's programs."""

    bin_dir: Path
    version: str            # "18.7"
    major: int
    minor: int = 0
    suffix: str = ""        # ".exe" on Windows

    def program(self, name):
        return self.bin_dir / (name + self.suffix)


def candidate_dirs(system=None, env=None, root=None):
    """The folders to look in, in order: PATH, then the usual places of this system.

    `root` stands for the top of the file system (tests build a small tree there).
    """
    system = system or current_system()
    env = os.environ if env is None else env
    top = Path(root) if root is not None else Path(os.path.abspath(os.sep))
    found = []

    for part in (env.get("PATH") or "").split(os.pathsep):
        if part.strip():
            found.append(Path(part.strip().strip('"')))

    def glob(base, pattern):
        try:
            found.extend(sorted(Path(base).glob(pattern)))
        except (OSError, ValueError):
            pass

    def from_env(name, *more):
        value = env.get(name)
        return Path(value, *more) if value and value.strip() else None

    if system == "darwin":
        glob(top, "opt/homebrew/opt/postgresql@*/bin")
        glob(top, "usr/local/opt/postgresql@*/bin")
        glob(top, "Applications/Postgres.app/Contents/Versions/*/bin")
        glob(top, "Library/PostgreSQL/*/bin")
    elif system == "windows":
        for base in (from_env("ProgramFiles", "PostgreSQL"),
                     from_env("LOCALAPPDATA", "Programs", "PostgreSQL")):
            if base is not None:
                glob(base, "*/bin")
        for single in (from_env("PGBIN"), from_env("PGROOT", "bin"),
                       from_env("USERPROFILE", "scoop", "apps", "postgresql", "current", "bin"),
                       from_env("CONDA_PREFIX", "Library", "bin")):
            if single is not None:
                found.append(single)
    else:
        glob(top, "usr/lib/postgresql/*/bin")
        glob(top, "usr/pgsql-*/bin")
    return found


def read_version(postgres_program):
    """(major, minor, "18.7") from `postgres --version`, or None when it cannot be run."""
    try:
        result = subprocess.run([str(postgres_program), "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=20, env=child_environment())
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_version(result.stdout.decode("utf-8", "replace"))


def parse_version(text):
    """(18, 7, "18.7") for "postgres (PostgreSQL) 18.7 (Homebrew)"; None when it is not that."""
    match = _VERSION_TEXT.search(text or "")
    if not match:
        return None
    major, minor = int(match.group(1)), int(match.group(2) or 0)
    return major, minor, "%d.%d" % (major, minor)


def _same_folder(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def scan_pg_bins(explicit=None, *, env=None, system=None, root=None, version_of=None):
    """(usable, too_old): two lists of PgBin. See find_pg_bins."""
    system = system or current_system()
    env = os.environ if env is None else env
    version_of = version_of or read_version
    suffix = ".exe" if system == "windows" else ""
    if explicit is None:
        explicit = (env.get("PCLDB_PG_BIN") or "").strip() or None
    seen, first, rest, too_old = [], [], [], []

    def consider(folder, into):
        folder = Path(folder)
        key = os.path.normcase(os.path.realpath(folder))
        if key in seen:
            return
        seen.append(key)
        if not all((folder / (name + suffix)).is_file() for name in PROGRAMS):
            return
        version = version_of(folder / ("postgres" + suffix))
        if not version:
            return
        major, minor, text = version
        found = PgBin(folder, text, major, minor, suffix)
        (into if major >= MIN_MAJOR else too_old).append(found)

    if explicit:
        consider(Path(explicit).expanduser(), first)
    for folder in candidate_dirs(system, env, root):
        consider(folder, rest)
    rest.sort(key=lambda item: (item.major, item.minor), reverse=True)
    return first + rest, too_old


def find_pg_bins(explicit=None, *, env=None, system=None, root=None, version_of=None):
    """The usable PostgreSQL program folders: the explicit one first, then the newest first.

    Looked at, in this order: `explicit` (the setting database.postgresql.bin_dir; when it is
    None, the environment variable PCLDB_PG_BIN), the folders of PATH, then the usual places of
    this system. A folder counts only when initdb, pg_ctl and postgres are all in it (with
    `.exe` on Windows) and `postgres --version` says 16 or newer.

    The keyword arguments are for tests: `env` (instead of os.environ), `system` ("darwin",
    "windows", "linux"), `root` (a folder that stands for the top of the file system) and
    `version_of(path of postgres) -> (major, minor, text) or None`.
    """
    return scan_pg_bins(explicit, env=env, system=system, root=root, version_of=version_of)[0]


def choose_pg_bin(settings):
    """The PgBin this project uses, or a PclError that says what to install or to set.

    A folder that was asked for by name (the setting database.postgresql.bin_dir, or the
    environment variable PCLDB_PG_BIN) must be the one that is used: when the programs are not
    there, that is an error and never a silent change to another version.
    """
    explicit = (settings.database.postgresql.bin_dir
                or (os.environ.get("PCLDB_PG_BIN") or "").strip() or None)
    usable, too_old = scan_pg_bins(explicit)
    if explicit:
        if usable and _same_folder(usable[0].bin_dir, Path(explicit).expanduser()):
            return usable[0]
        old = [item for item in too_old if _same_folder(item.bin_dir, Path(explicit).expanduser())]
        if old:
            raise PclError(
                "The folder %s holds PostgreSQL %s; pcldb needs PostgreSQL %d or newer. Point "
                "database.postgresql.bin_dir (or PCLDB_PG_BIN) at a newer version."
                % (explicit, old[0].version, MIN_MAJOR), code="postgres_missing", status=503)
        raise PclError(
            "PostgreSQL's programs (initdb, pg_ctl, postgres) were not found in %s. That folder "
            "comes from the setting database.postgresql.bin_dir (or PCLDB_PG_BIN): correct it, "
            "or remove it to let pcldb look in the usual places." % explicit,
            code="postgres_missing", status=503)
    if usable:
        return usable[0]
    if too_old:
        raise PclError(
            "Only PostgreSQL %s was found (%s); pcldb needs PostgreSQL %d or newer. %s"
            % (too_old[0].version, too_old[0].bin_dir, MIN_MAJOR, _what_to_do(settings)),
            code="postgres_missing", status=503)
    raise PclError(_NOT_FOUND + _what_to_do(settings), code="postgres_missing", status=503)


# ---------------------------------------------------------------------- child processes


def child_environment(env=None, system=None):
    """The environment for PostgreSQL's programs: no PG* variables, and a locale on macOS.

    Every variable whose name starts with PG goes (PGDATA, PGPORT, PGUSER, ... and also PGBIN or
    PGROOT, which installers set), in any mix of capitals: names on Windows ignore case."""
    result = {name: value for name, value in (os.environ if env is None else env).items()
              if not name.upper().startswith("PG")}
    if (system or current_system()) == "darwin":
        if not (result.get("LC_ALL") or "").strip() and not (result.get("LANG") or "").strip():
            # Without any locale the server stops with "postmaster became multithreaded during
            # startup". The cluster's own locale stays C (set by initdb), whatever this is.
            result["LC_ALL"] = "en_US.UTF-8"
    return result


def _tail(path, lines=20):
    """The last lines of a log file (only its end is read, however big it is)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:]).rstrip()


# ---------------------------------------------------------------------- the generated passwords


def read_local_secrets(settings):
    """{"owner", "app", "reader"}: the local cluster's passwords. Never print them."""
    path = settings.local_secrets_path
    if not path.is_file():
        raise PclError(
            "The passwords of the local PostgreSQL database are missing (%s). If the database "
            "was never set up, run: pcldb setup. If the file was deleted, the database in "
            "var/pgdata cannot be opened any more: run pcldb db reset --yes to start over."
            % path, code="db_secrets_missing", status=503)
    data = util.read_json(path)
    if not isinstance(data, dict) or not all(
            isinstance(data.get(role), str) and data.get(role) for role in SECRET_ROLES):
        raise PclError("%s is damaged: it must hold the three passwords owner, app and reader. "
                       "Run pcldb db reset --yes to start over." % path,
                       code="db_secrets_missing", status=503)
    return {role: data[role] for role in SECRET_ROLES}


def ensure_local_secrets(settings):
    """The local cluster's passwords; made (random, mode 0600) when the file is missing."""
    path = settings.local_secrets_path
    if path.is_file():
        return read_local_secrets(settings)
    data = {"_about": "Passwords of the local PostgreSQL database in var/pgdata, made by pcldb. "
                      "Keep this file private and never commit it."}
    for role in SECRET_ROLES:
        data[role] = secrets.token_urlsafe(24)
    util.write_json_atomic(path, data, mode=0o600)
    return {role: data[role] for role in SECRET_ROLES}


# ---------------------------------------------------------------------- the cluster


class LocalCluster:
    """The PostgreSQL cluster in `var/pgdata` of one project folder."""

    def __init__(self, settings, *, test_mode=False):
        self.settings = settings
        self.data_dir = settings.pgdata_dir
        self.log_dir = settings.log_dir
        self.log_file = self.log_dir / "postgresql.log"   # the server's output while it starts
        self.tool_log = self.log_dir / "pg_ctl.log"       # what pg_ctl itself printed
        # Once it runs, the server writes its own log files into var/pgdata/log.
        self.port = settings.database.postgresql.port
        self.test_mode = test_mode      # throwaway clusters of the test suite: no fsync
        self._bin = None
        self.notices = []               # sentences for the person, filled by init()

    # -- facts -----------------------------------------------------------------------------------

    @property
    def pg_bin(self):
        if self._bin is None:
            self._bin = choose_pg_bin(self.settings)
        return self._bin

    def is_initialized(self):
        return (self.data_dir / "PG_VERSION").is_file()

    def data_version(self):
        """The major version that made the data folder ("18"), or None."""
        try:
            return (self.data_dir / "PG_VERSION").read_text(encoding="ascii").strip() or None
        except (OSError, UnicodeDecodeError):
            return None

    def _pid_file(self):
        """(pid, port) from postmaster.pid, or None when there is no usable one."""
        try:
            lines = (self.data_dir / "postmaster.pid").read_text(
                encoding="utf-8", errors="replace").splitlines()
            return int(lines[0].strip()), int(lines[3].strip())
        except (OSError, ValueError, IndexError):
            return None

    def _no_response(self, port):
        """True when no PostgreSQL server answers on 127.0.0.1:port (pg_isready says 2)."""
        if util.port_answers(port):
            return False
        try:
            program = self.pg_bin.program("pg_isready")
        except PclError:
            program = None
        if program is not None and program.is_file():
            code = self._run([program, "-h", "127.0.0.1", "-p", str(port), "-t", "3"], quiet=True)
            return code == 2
        return not util.port_answers(port)

    def _pid_is_another_program(self, pid):
        """True when the process with this pid is surely not PostgreSQL (a recycled pid)."""
        if current_system() == "windows":
            return False    # there pg_ctl only counts real PostgreSQL processes
        try:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        name = result.stdout.decode("utf-8", "replace").strip()
        return result.returncode == 0 and bool(name) and "postgres" not in name.lower()

    def status(self):
        """{"initialized", "running", "pid", "port", "version", "data_dir", "bin_dir"}.

        Never raises because PostgreSQL's programs are missing: then `bin_dir` is None.
        """
        try:
            pg = self.pg_bin
        except PclError:
            pg = None
        initialized = self.is_initialized()
        running, pid, port = False, None, self.port
        found = self._pid_file() if initialized else None
        if found is not None:
            if pg is not None:
                # pg_ctl status: 0 = running, 3 = not running, 4 = no usable data folder.
                running = self._run([pg.program("pg_ctl"), "status", "-D", str(self.data_dir)],
                                    quiet=True) == 0
                if running and self._no_response(found[1]) \
                        and self._pid_is_another_program(found[0]):
                    running = False     # a left-over pid file whose pid another program has now
            else:
                running = util.port_answers(found[1])
            if running:
                pid, port = found
        return {
            "initialized": initialized,
            "running": running,
            "pid": pid,
            "port": port,
            "version": pg.version if pg is not None else self.data_version(),
            "data_dir": str(self.data_dir),
            "bin_dir": str(pg.bin_dir) if pg is not None else None,
        }

    # -- running the programs --------------------------------------------------------------------

    def _run(self, args, *, quiet=False, detached=False, timeout=120):
        """Run one of PostgreSQL's programs without a pipe; its exit code.

        Output goes to var/log/pg_ctl.log (or nowhere with `quiet`). No pipe is ever handed to
        `pg_ctl start`: the server would inherit it.
        """
        flags = 0
        if detached and current_system() == "windows":
            # Its own hidden console and process group: Ctrl+C in the terminal that runs
            # `pcldb serve`, or closing that window, must not stop the database.
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        command = [str(part) for part in args]
        try:
            if quiet:
                return subprocess.run(command, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      env=child_environment(), timeout=timeout,
                                      creationflags=flags).returncode
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.tool_log, "ab") as output:
                output.write(("\n[%s] %s\n" % (util.now_utc_text(),
                                               " ".join(command[:2]))).encode("utf-8"))
                output.flush()
                return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output,
                                      stderr=subprocess.STDOUT, env=child_environment(),
                                      timeout=timeout, creationflags=flags).returncode
        except subprocess.TimeoutExpired:
            raise PclError("%s did not finish within %d seconds. Look at %s."
                           % (Path(command[0]).name, timeout, self.log_file),
                           code="postgres_failed", status=503) from None
        except OSError as error:
            raise PclError("%s could not be run (%s)." % (command[0], error.strerror or error),
                           code="postgres_failed", status=503) from None

    def _conf_block(self):
        lines = [
            _CONF_BEGIN,
            "listen_addresses = '127.0.0.1'",
            "port = %d" % self.port,
            "unix_socket_directories = ''",
            "timezone = 'UTC'",
            "log_timezone = 'UTC'",
            "logging_collector = on",
            "log_directory = 'log'",
        ]
        if self.test_mode:
            lines += ["fsync = off", "synchronous_commit = off", "full_page_writes = off"]
        lines.append(_CONF_END)
        return "\n".join(lines) + "\n"

    def _write_conf(self):
        """Put pcldb's block at the end of postgresql.conf (later settings win)."""
        path = self.data_dir / "postgresql.conf"
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            text = handle.read()
        base = _CONF_BLOCK.sub("\n", text).rstrip("\r\n")
        wanted = base + "\n\n" + self._conf_block()
        if wanted != text:
            util.write_text_atomic(path, wanted, mode=0o600)

    # -- create, start, stop -----------------------------------------------------------------

    def init(self):
        """Create the cluster when there is none. True when it was created now."""
        if self.is_initialized():
            return False
        pg = self.pg_bin
        if self.data_dir.exists():
            # initdb makes the folder itself, and refuses one that holds anything: even the
            # .DS_Store that the macOS Finder leaves behind when someone looks into it.
            inside = [path.name for path in self.data_dir.iterdir()]
            if any(name not in (".DS_Store", "Thumbs.db", "desktop.ini") for name in inside):
                raise PclError(
                    "%s exists but is not a PostgreSQL data folder. Move it away or delete it, "
                    "then run the command again." % self.data_dir, code="postgres_failed",
                    status=503)
            shutil.rmtree(self.data_dir, ignore_errors=True)
        passwords = ensure_local_secrets(self.settings)
        self.settings.var_dir.mkdir(parents=True, exist_ok=True)
        password_file = self.settings.var_dir / (".initdb-password.%s.tmp" % secrets.token_hex(4))
        output_file = self.log_dir / "initdb.log"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        command = [pg.program("initdb"), "-D", self.data_dir, "-U", OWNER_ROLE,
                   "--pwfile=%s" % password_file, "--auth=scram-sha-256", "--encoding=UTF8"]
        if pg.major >= 17:
            command += ["--locale-provider=builtin", "--builtin-locale=C.UTF-8"]
        else:
            self.notices.append(
                "PostgreSQL %s has no built-in Unicode locale, so this database uses plain C "
                "rules: upper() and lower() change the letters A to Z only." % pg.version)
        command += ["--locale=C", "--no-instructions"]
        if self.test_mode:
            command.append("--no-sync")
        try:
            util.write_text_atomic(password_file, passwords["owner"] + "\n", mode=0o600)
            with open(output_file, "wb") as output:
                try:
                    code = subprocess.run([str(part) for part in command],
                                          stdin=subprocess.DEVNULL, stdout=output,
                                          stderr=subprocess.STDOUT, env=child_environment(),
                                          timeout=600).returncode
                except (OSError, subprocess.SubprocessError) as error:
                    output.write(("\n%s\n" % error).encode("utf-8", "replace"))
                    code = -1
        finally:
            try:
                password_file.unlink()
            except OSError:
                pass
        if code != 0 or not self.is_initialized():
            shutil.rmtree(self.data_dir, ignore_errors=True)
            said = _tail(output_file)
            hint = ""
            if current_system() == "windows" and "Permission denied" in said:
                hint = ("\nOn Windows, PostgreSQL's programs run with reduced rights and cannot "
                        "use a folder that only administrators may open. Put the project in a "
                        "folder in your own user profile (for example C:\\Users\\<you>\\pcldb) "
                        "and run the command again.")
            raise PclError(
                "PostgreSQL could not create its data folder %s. What initdb said (%s):\n%s%s"
                % (self.data_dir, output_file, said, hint), code="postgres_failed",
                status=503)
        self._write_conf()
        return True

    def _check_before_start(self, pg):
        made_by = self.data_version()
        if made_by != str(pg.major):
            raise PclError(
                "The database folder %s was made by PostgreSQL %s, but the programs in %s are "
                "PostgreSQL %s. Set database.postgresql.bin_dir (or PCLDB_PG_BIN) to the folder "
                "of PostgreSQL %s's programs. To move to the newer version instead: pcldb dump, "
                "then pcldb db reset --yes, then pcldb restore."
                % (self.data_dir, made_by, pg.bin_dir, pg.version, made_by),
                code="postgres_version_mismatch", status=503)
        if util.port_in_use(self.port):
            raise PclError(
                "Port %d on this computer is already in use, so the local PostgreSQL database "
                "cannot start. If another pcldb folder is running there, stop it (pcldb db "
                "stop); otherwise change database.postgresql.port in pcldb_config.json to a "
                "free port." % self.port, code="port_in_use", status=503)

    def _rotate_log(self):
        try:
            if self.log_file.stat().st_size > 5 * 1024 * 1024:
                os.replace(self.log_file, self.log_file.with_name(self.log_file.name + ".1"))
        except OSError:
            pass

    def start(self):
        """Start the server when it is not running. True when it was started now."""
        status = self.status()
        if not status["initialized"]:
            raise PclError("The local PostgreSQL database has not been created yet. Run: "
                           "pcldb setup --engine postgresql", code="not_initialized", status=503)
        if status["running"]:
            return False
        pg = self.pg_bin
        self._check_before_start(pg)
        self._write_conf()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._rotate_log()
        wait = 120 if current_system() == "windows" else 60
        command = [pg.program("pg_ctl"), "start", "-D", self.data_dir, "-l", self.log_file,
                   "-w", "-t", str(wait)]
        code = self._run(command, detached=True, timeout=wait + 90)
        if code != 0 and self._remove_left_over_pid_file():
            code = self._run(command, detached=True, timeout=wait + 90)
        if code != 0:
            raise PclError("The local PostgreSQL database did not start. %s"
                           % self._why_it_failed(), code="postgres_failed", status=503)
        return True

    def _remove_left_over_pid_file(self):
        """After a failed start: remove postmaster.pid when it is surely left over. True when it
        was removed (the caller then tries the start once more)."""
        found = self._pid_file()
        if found is None or not self._no_response(found[1]):
            return False        # no pid file, or a server answers: never touch it
        if not self._pid_is_another_program(found[0]):
            return False        # the process could be PostgreSQL (or is gone: the server's job)
        try:
            (self.data_dir / "postmaster.pid").unlink()
        except OSError:
            return False
        return True

    def _server_log(self):
        """The newest file the server's own log collector wrote (var/pgdata/log), or None."""
        try:
            files = [path for path in (self.data_dir / "log").iterdir() if path.is_file()]
        except OSError:
            return None
        return max(files, key=lambda path: path.stat().st_mtime) if files else None

    def _why_it_failed(self):
        text = "The last lines of %s:\n%s" % (self.log_file,
                                              _tail(self.log_file) or "(the file is empty)")
        newest = self._server_log()
        if newest is not None and _tail(newest):
            text += "\nThe last lines of the server's own log, %s:\n%s" % (newest, _tail(newest))
        return text

    def ensure_running(self):
        """Make sure the server runs. True when this call started it (the caller may then stop
        it again when it is done)."""
        return self.start()

    def stop(self):
        """Stop the server. True when it was running. Not running is not an error."""
        status = self.status()
        if not status["initialized"] or not status["running"]:
            return False
        wait = 120 if current_system() == "windows" else 60
        code = self._run([self.pg_bin.program("pg_ctl"), "stop", "-D", self.data_dir, "-m",
                          "fast", "-w", "-t", str(wait)], timeout=wait + 90)
        if code != 0 and self.status()["running"]:
            raise PclError(
                "The local PostgreSQL database did not stop. The last lines of %s:\n%s"
                % (self.tool_log, _tail(self.tool_log)), code="postgres_failed", status=503)
        return True

    def destroy(self):
        """Stop the server and delete the data folder. The passwords file and the logs stay."""
        if self.is_initialized():
            self.stop()
        if self.data_dir.exists():
            shutil.rmtree(self.data_dir, ignore_errors=True)
            if self.data_dir.exists():
                raise PclError("%s could not be deleted completely. Close any program that "
                               "uses it and try again." % self.data_dir, code="file_in_use",
                               status=503)
