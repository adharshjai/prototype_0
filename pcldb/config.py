"""Settings, paths and secrets.

Where things are:

- The project folder P holds the `pcldb` package (or is `PCLDB_DIR`). Local state lives in
  `P/var/`. The settings file is `P/pcldb_config.json`; `pcldb_config.example.json` documents
  every key.
- The per-user folder U is `~/.pcldb` (or `PCLDB_USER_DIR`). It holds what belongs to a person:
  `arc_llm_api_key` and `globus_tokens.json`. Nothing of that kind lives in the project folder.

`load_settings` reads the defaults, then the settings file, then the environment; each later
source wins. A key the program does not know is an error that names the key. In the settings
file every key that starts with an underscore (`"_about"`) is a note and is ignored, at every
level: JSON has no comments, so notes are written that way.

Settings are frozen dataclasses: `settings.database.engine`, `settings.sql.max_rows` ... Lists
are tuples. `repr()` never shows a secret: connection strings, the token and the LLM key are
left out.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import util
from .errors import PclError

CONFIG_NAME = "pcldb_config.json"
EXAMPLE_NAME = "pcldb_config.example.json"
ARC_PROJECTS_COLLECTION = "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"
# The public "native app" client id of the Globus SDK tutorial; not a person's id.
GLOBUS_TUTORIAL_CLIENT = "61338d24-54d5-408f-a10d-66c06b59f6d2"

ENGINES = ("postgresql", "sqlite")

# Every key with its default. This is also the shape of the settings file.
DEFAULTS = {
    "database": {
        "engine": "postgresql",
        "postgresql": {
            "mode": "local",
            "bin_dir": None,
            "port": 15439,
            "name": "pcl",
            "external": {"owner": None, "app": None, "reader": None},
        },
        "sqlite": {"folder": "var/sqlite"},
    },
    "server": {"host": "127.0.0.1", "port": 8787, "allowed_hosts": []},
    "storage": {
        "collection_id": ARC_PROJECTS_COLLECTION,
        "collection_root": "/projects",
        "project_path": "/projects/pcl_photonics",
        "ssh_host": "datatransfer.arc.vt.edu",
    },
    "search": {
        "backend": "local",
        "index_id": None,
        "visible_to": [],
        "client_id": GLOBUS_TUTORIAL_CLIENT,
        "include_operator": False,
        "include_notes": False,
        "fallback_to_local": True,
    },
    "llm": {
        "enabled": True,
        "base_url": "https://llm-api.arc.vt.edu/api/v1",
        "model": "gpt-oss-120b",
        "reasoning_effort": "low",
        "response_format": False,
        "max_concurrent": 2,
        "api_key_file": None,
        "timeout_seconds": 90,
    },
    "ask": {"include_simulated_default": True, "max_results": 20, "per_user_per_minute": 20},
    "sql": {"max_rows": 1000, "timeout_seconds": 15, "max_cell_chars": 10000,
            "max_sql_chars": 20000},
}

# Environment variables that override one key of the file.
ENV_KEYS = {
    "PCLDB_ENGINE": "database.engine",
    "PCLDB_PG_BIN": "database.postgresql.bin_dir",
    "PCLDB_PG_OWNER_DSN": "database.postgresql.external.owner",
    "PCLDB_PG_APP_DSN": "database.postgresql.external.app",
    "PCLDB_PG_READER_DSN": "database.postgresql.external.reader",
    "PCLDB_LLM_BASE_URL": "llm.base_url",
    "PCLDB_LLM_MODEL": "llm.model",
}
_DSN_VARIABLES = ("PCLDB_PG_OWNER_DSN", "PCLDB_PG_APP_DSN", "PCLDB_PG_READER_DSN")

# Keys whose value is a secret: never shown by repr() or to_dict().
SECRET_KEYS = frozenset({
    "database.postgresql.external.owner",
    "database.postgresql.external.app",
    "database.postgresql.external.reader",
})


# ---------------------------------------------------------------------- checking one value


def _bad(key, value, need, origin):
    shown = "not usable" if key in SECRET_KEYS else util._show(value)  # never echo a secret
    return PclError("The setting %s is %s; it must be %s.%s" % (key, shown, need, origin),
                    code="bad_setting", details={"key": key})


def _choice(*allowed):
    def check(value, key, origin):
        if not isinstance(value, str) or value not in allowed:
            raise _bad(key, value, " or ".join('"%s"' % item for item in allowed), origin)
        return value
    return check


def _text(value, key, origin):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _bad(key, value, "text that is not empty", origin)
    return value


def _any_text(value, key, origin):
    if not isinstance(value, str) or "\x00" in value:
        raise _bad(key, value, 'text (it may be empty: "")', origin)
    return value


def _optional_text(value, key, origin):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _bad(key, value, "text, or null when it is not used", origin)
    return value


def _flag(value, key, origin):
    if not isinstance(value, bool):
        raise _bad(key, value, "true or false", origin)
    return value


def _whole(low, high):
    def check(value, key, origin):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise _bad(key, value, "a whole number from %d to %d" % (low, high), origin)
        return value
    return check


def _seconds(value, key, origin):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
        raise _bad(key, value, "a number of seconds above 0 and at most 3600", origin)
    return value


def _text_list(value, key, origin):
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() and "\x00" not in item for item in value):
        raise _bad(key, value, "a list of texts, for example []", origin)
    return tuple(value)


_DATABASE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _database_name(value, key, origin):
    if not isinstance(value, str) or not _DATABASE_NAME.fullmatch(value):
        raise _bad(key, value, "a plain database name: lower-case letters, digits and "
                               "underscores, starting with a letter", origin)
    return value


def _folder_path(value, key, origin):
    """A POSIX path on ARC or inside the Globus collection: starts with /, no trailing /."""
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or "\\" in value:
        raise _bad(key, value, "a path that starts with /, for example /projects", origin)
    return value.rstrip("/") or "/"


def _web_address(value, key, origin):
    if not isinstance(value, str) or not re.match(r"https?://[^\s/]+", value):
        raise _bad(key, value, "a web address that starts with http:// or https://", origin)
    return value.rstrip("/")


_PORT = _whole(1, 65535)

# Dotted key -> the check for its value. A key that is not here is unknown.
_CHECKS = {
    "database.engine": _choice(*ENGINES),
    "database.postgresql.mode": _choice("local", "external"),
    "database.postgresql.bin_dir": _optional_text,
    "database.postgresql.port": _PORT,
    "database.postgresql.name": _database_name,
    "database.postgresql.external.owner": _optional_text,
    "database.postgresql.external.app": _optional_text,
    "database.postgresql.external.reader": _optional_text,
    "database.sqlite.folder": _text,
    "server.host": _text,
    "server.port": _PORT,
    "server.allowed_hosts": _text_list,
    "storage.collection_id": _text,
    "storage.collection_root": _folder_path,
    "storage.project_path": _folder_path,
    "storage.ssh_host": _text,
    "search.backend": _choice("local", "globus"),
    "search.index_id": _optional_text,
    "search.visible_to": _text_list,
    "search.client_id": _text,
    "search.include_operator": _flag,
    "search.include_notes": _flag,
    "search.fallback_to_local": _flag,
    "llm.enabled": _flag,
    "llm.base_url": _web_address,
    "llm.model": _any_text,
    "llm.reasoning_effort": _any_text,
    "llm.response_format": _flag,
    "llm.max_concurrent": _whole(1, 64),
    "llm.api_key_file": _optional_text,
    "llm.timeout_seconds": _seconds,
    "ask.include_simulated_default": _flag,
    "ask.max_results": _whole(1, 1000),
    "ask.per_user_per_minute": _whole(1, 100000),
    "sql.max_rows": _whole(1, 1000000),
    "sql.timeout_seconds": _seconds,
    "sql.max_cell_chars": _whole(1, 10000000),
    "sql.max_sql_chars": _whole(1, 10000000),
}


def _sections(tree, prefix=""):
    names = set()
    for key, value in tree.items():
        if isinstance(value, dict):
            names.add(prefix + key)
            names |= _sections(value, prefix + key + ".")
    return names


_SECTIONS = _sections(DEFAULTS)


def _set(tree, dotted, value):
    parts = dotted.split(".")
    for part in parts[:-1]:
        tree = tree[part]
    tree[parts[-1]] = value


def _get(tree, dotted):
    for part in dotted.split("."):
        tree = tree[part]
    return tree


def _unknown(key, origin):
    return PclError(
        "%s is not a setting this version of pcldb knows.%s Check the spelling against %s, or "
        "remove it." % (key, origin, EXAMPLE_NAME), code="unknown_setting", details={"key": key})


def _merge(target, given, origin, prefix=""):
    """Copy the checked values of `given` (the settings file's content) into `target`."""
    for key, value in given.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue  # a note for people; ignored by design
        dotted = prefix + key
        if dotted in _SECTIONS:
            if not isinstance(value, dict):
                raise _bad(dotted, value, "a group of settings written in { }", origin)
            _merge(target[key], value, origin, dotted + ".")
        elif dotted in _CHECKS:
            target[key] = _plain(_CHECKS[dotted](value, dotted, origin))
        else:
            raise _unknown(dotted, origin)


def _plain(value):
    return list(value) if isinstance(value, tuple) else value


def _apply_changes(data, changes, origin):
    for dotted, value in (changes or {}).items():
        if dotted not in _CHECKS:
            raise _unknown(dotted, origin)
        _set(data, dotted, _plain(_CHECKS[dotted](value, dotted, origin)))


def _check_together(data, origin):
    storage = data["storage"]
    root, project = storage["collection_root"], storage["project_path"]
    if not (project == root or root == "/" or project.startswith(root + "/")):
        raise PclError(
            "The setting storage.project_path (%s) must be inside storage.collection_root (%s): "
            "the Globus collection shows the project folder below its root.%s"
            % (project, root, origin), code="bad_setting")


# ---------------------------------------------------------------------- the settings tree


@dataclass(frozen=True)
class PostgresExternal:
    """The three connection strings of an external PostgreSQL. They hold passwords."""

    owner: str | None = None
    app: str | None = None
    reader: str | None = None

    def __repr__(self):
        shown = ", ".join("%s=%s" % (name, "<hidden>" if getattr(self, name) else "None")
                          for name in ("owner", "app", "reader"))
        return "PostgresExternal(%s)" % shown


@dataclass(frozen=True)
class PostgresSettings:
    mode: str
    bin_dir: str | None
    port: int
    name: str
    external: PostgresExternal


@dataclass(frozen=True)
class SqliteSettings:
    folder: str


@dataclass(frozen=True)
class DatabaseSettings:
    engine: str
    postgresql: PostgresSettings
    sqlite: SqliteSettings


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    allowed_hosts: tuple


@dataclass(frozen=True)
class StorageSettings:
    collection_id: str
    collection_root: str
    project_path: str
    ssh_host: str


@dataclass(frozen=True)
class SearchSettings:
    backend: str
    index_id: str | None
    visible_to: tuple
    client_id: str
    include_operator: bool
    include_notes: bool
    fallback_to_local: bool


@dataclass(frozen=True)
class LlmSettings:
    enabled: bool
    base_url: str
    model: str
    reasoning_effort: str
    response_format: bool
    max_concurrent: int
    api_key_file: str | None
    timeout_seconds: float


@dataclass(frozen=True)
class AskSettings:
    include_simulated_default: bool
    max_results: int
    per_user_per_minute: int


@dataclass(frozen=True)
class SqlSettings:
    max_rows: int
    timeout_seconds: float
    max_cell_chars: int
    max_sql_chars: int


def _first_line(path):
    """The first line of a small text file, or None when there is no usable one."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            line = handle.readline(8192).strip()
    except (OSError, UnicodeDecodeError):
        return None
    return line or None


@dataclass(frozen=True)
class Settings:
    """Everything pcldb is told from outside. Build with load_settings() or Settings.for_test()."""

    project_dir: Path
    user_dir: Path
    database: DatabaseSettings
    server: ServerSettings
    storage: StorageSettings
    search: SearchSettings
    llm: LlmSettings
    ask: AskSettings
    sql: SqlSettings
    server_url: str | None = None                                  # PCLDB_SERVER_URL
    token: str | None = field(default=None, repr=False)            # PCLDB_TOKEN (a secret)
    env_llm_api_key: str | None = field(default=None, repr=False)  # PCLDB_LLM_API_KEY (a secret)

    # ------------------------------------------------------------------ paths

    @property
    def var_dir(self):
        """P/var: databases, logs, generated passwords. Git-ignored."""
        return self.project_dir / "var"

    @property
    def log_dir(self):
        return self.var_dir / "log"

    @property
    def config_path(self):
        return self.project_dir / CONFIG_NAME

    @property
    def sqlite_dir(self):
        """The folder of the two SQLite files (database.sqlite.folder, relative to P)."""
        folder = Path(self.database.sqlite.folder).expanduser()
        return folder if folder.is_absolute() else self.project_dir / folder

    @property
    def pgdata_dir(self):
        """The local PostgreSQL cluster's data folder."""
        return self.var_dir / "pgdata"

    @property
    def local_secrets_path(self):
        """The generated passwords of the local PostgreSQL cluster."""
        return self.var_dir / "local_secrets.json"

    @property
    def llm_api_key_path(self):
        return self.user_dir / "arc_llm_api_key"

    @property
    def globus_tokens_path(self):
        return self.user_dir / "globus_tokens.json"

    # ------------------------------------------------------------------ secrets

    def llm_api_key(self):
        """The ARC LLM key: PCLDB_LLM_API_KEY, else llm.api_key_file, else U/arc_llm_api_key,
        else None. Never print or log what this returns."""
        if self.env_llm_api_key:
            return self.env_llm_api_key
        candidates = []
        if self.llm.api_key_file:
            configured = Path(self.llm.api_key_file).expanduser()
            candidates.append(configured if configured.is_absolute()
                              else self.project_dir / configured)
        candidates.append(self.llm_api_key_path)
        for path in candidates:
            key = _first_line(path)
            if key:
                return key
        return None

    # ------------------------------------------------------------------ building

    @classmethod
    def _build(cls, data, project_dir, user_dir, *, server_url=None, token=None,
               env_llm_api_key=None, origin=""):
        checked = {}
        for dotted, check in _CHECKS.items():
            checked[dotted] = check(_get(data, dotted), dotted, origin)
        tree = copy.deepcopy(data)
        for dotted, value in checked.items():
            _set(tree, dotted, value)
        _check_together(tree, origin)
        database = tree["database"]
        postgresql = database["postgresql"]
        return cls(
            project_dir=Path(os.path.abspath(project_dir)),
            user_dir=Path(os.path.abspath(user_dir)),
            database=DatabaseSettings(
                engine=database["engine"],
                postgresql=PostgresSettings(
                    mode=postgresql["mode"], bin_dir=postgresql["bin_dir"],
                    port=postgresql["port"], name=postgresql["name"],
                    external=PostgresExternal(**postgresql["external"])),
                sqlite=SqliteSettings(**database["sqlite"])),
            server=ServerSettings(**tree["server"]),
            storage=StorageSettings(**tree["storage"]),
            search=SearchSettings(**tree["search"]),
            llm=LlmSettings(**tree["llm"]),
            ask=AskSettings(**tree["ask"]),
            sql=SqlSettings(**tree["sql"]),
            server_url=server_url or None,
            token=token or None,
            env_llm_api_key=env_llm_api_key or None,
        )

    def _data(self):
        """The nested dict this was built from, secrets included. For rebuilding only."""
        data = copy.deepcopy(DEFAULTS)
        for dotted in _CHECKS:
            value = self
            for part in dotted.split("."):
                value = getattr(value, part)
            _set(data, dotted, _plain(value))
        return data

    def to_dict(self):
        """The settings as a nested dict that is safe to show: secrets read "<hidden>"."""
        data = self._data()
        for dotted in SECRET_KEYS:
            if _get(data, dotted):
                _set(data, dotted, "<hidden>")
        return data

    def changed(self, changes):
        """A copy with some keys changed: settings.changed({"sql.max_rows": 5})."""
        data = self._data()
        _apply_changes(data, changes, "")
        return Settings._build(data, self.project_dir, self.user_dir, server_url=self.server_url,
                               token=self.token, env_llm_api_key=self.env_llm_api_key)

    @classmethod
    def for_test(cls, tmp_dir, engine="sqlite", changes=None):
        """Settings for a test, isolated in the temporary folder `tmp_dir`.

        Nothing is read from the settings file, the environment or the home folder. The project
        folder is `tmp_dir`, the per-user folder `tmp_dir/user`. Both ports are free ports picked
        now (never 8787 or 15439), and the LLM address points at a closed local port so that a
        test which forgets its fake server fails at once instead of reaching the network.
        `changes` are dotted keys: {"sql.max_rows": 5}.
        """
        tmp_dir = Path(tmp_dir)
        data = copy.deepcopy(DEFAULTS)
        data["database"]["engine"] = engine
        data["database"]["postgresql"]["port"] = util.free_port()
        data["server"]["port"] = util.free_port()
        data["llm"]["base_url"] = "http://127.0.0.1:9/api/v1"
        _apply_changes(data, changes, "")
        return cls._build(data, tmp_dir, tmp_dir / "user")


# ---------------------------------------------------------------------- loading


def default_project_dir():
    """The folder that holds the pcldb package."""
    return Path(os.path.abspath(__file__)).parent.parent


def load_settings(project_dir=None, user_dir=None, env=os.environ):
    """Defaults, then P/pcldb_config.json, then the environment variables. See the module text."""
    def from_env(name):
        value = env.get(name)
        return value if isinstance(value, str) and value.strip() else None

    if project_dir is None:
        project_dir = from_env("PCLDB_DIR") or default_project_dir()
    project_dir = Path(os.path.abspath(Path(project_dir).expanduser()))
    if user_dir is None:
        user_dir = from_env("PCLDB_USER_DIR")
    if user_dir is None:
        try:
            user_dir = Path.home() / ".pcldb"
        except (RuntimeError, KeyError):
            user_dir = project_dir / "var" / "user"
    user_dir = Path(user_dir).expanduser()

    data = copy.deepcopy(DEFAULTS)
    config_path = project_dir / CONFIG_NAME
    if config_path.exists():
        origin = " (in %s)" % config_path
        given = util.read_json(config_path)
        if not isinstance(given, dict):
            raise PclError("%s must hold one group of settings written in { }. See %s."
                           % (config_path, EXAMPLE_NAME), code="bad_setting")
        _merge(data, given, origin)

    for name, dotted in ENV_KEYS.items():
        value = from_env(name)
        if value is not None:
            origin = " (from the environment variable %s)" % name
            _set(data, dotted, _plain(_CHECKS[dotted](value, dotted, origin)))
    if any(from_env(name) for name in _DSN_VARIABLES):
        # A connection string from the environment means: use that PostgreSQL server.
        data["database"]["postgresql"]["mode"] = "external"

    return Settings._build(
        data, project_dir, user_dir,
        server_url=from_env("PCLDB_SERVER_URL"),
        token=from_env("PCLDB_TOKEN"),
        env_llm_api_key=from_env("PCLDB_LLM_API_KEY"),
        origin=" (settings of %s)" % project_dir)
