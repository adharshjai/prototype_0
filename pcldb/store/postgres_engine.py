"""The PostgreSQL engine (specification section 4.5).

Three roles:

- `pcl_owner` owns everything. Only `initialize()`, `destroy()` and `reset()` sign in as it;
  the running server never does.
- `pcl_app` reads and writes the schemas `pcl` and `app`, and cannot change their structure.
  `app()` signs in as it.
- `pcl_reader` may only read `pcl`. Expert SQL runs as it (`reader()`, section 9).

Local mode talks to the project's own cluster (pgcluster.py) on 127.0.0.1 with the generated
passwords of `var/local_secrets.json`. External mode uses the three connection strings of the
settings; there the roles `pcl_app` and `pcl_reader` must already exist.

psycopg is imported only when a connection is made. One connection per call, closed when the
`with` block ends; no pool. Values go in and come out as section 4.2 says: JSONB through
`Jsonb`, lists as `text[]`, times as aware datetimes that come back as the fixed-width UTC text.

Nothing here puts a password or a connection string into an error message.
"""

from __future__ import annotations

import contextlib
import decimal
import os
import re
from pathlib import Path

from .. import util
from ..errors import PclError, Unavailable
from . import pg_hardening, pgcluster, schema_def
from .base import (DatabaseError, Db, IntegrityError, Store, decimal_number,
                   not_initialized_error, one_block_at_a_time)

OWNER_ROLE = "pcl_owner"
APP_ROLE = "pcl_app"
READER_ROLE = "pcl_reader"
ROLES = {"owner": OWNER_ROLE, "app": APP_ROLE, "reader": READER_ROLE}
CONNECT_TIMEOUT_SECONDS = 5

_SETTING_NAMES = {"owner": "PCLDB_PG_OWNER_DSN", "app": "PCLDB_PG_APP_DSN",
                  "reader": "PCLDB_PG_READER_DSN"}

# PostgreSQL type numbers (pg_type.oid) of result columns that need converting.
_TIMESTAMPTZ, _TIMESTAMP, _DATE, _NUMERIC = 1184, 1114, 1082, 1700

_CONSTRAINT_KINDS = {"23505": "unique", "23502": "not_null", "23503": "foreign_key",
                     "23514": "check"}

# What the server says when a connection is refused for a reason that setup can repair.
_NO_SUCH_ROLE = re.compile(r'role "[^"]*" does not exist')
_NO_SUCH_DATABASE = re.compile(r'database "[^"]*" does not exist')


def _is_set_up(settings):
    """True when the project already has its settings file (it was set up). Without
    `settings`, the project folder is the one pcldb.config would use."""
    config_path = getattr(settings, "config_path", None)
    if config_path is None:
        from ..config import CONFIG_NAME, default_project_dir
        folder = os.environ.get("PCLDB_DIR", "").strip() or default_project_dir()
        config_path = Path(folder).expanduser() / CONFIG_NAME
    try:
        return config_path.exists()
    except OSError:
        return False


def load_psycopg(settings=None):
    """The psycopg module, or a PclError that says how to get it. The way out is pgcluster's: a
    new project may still choose SQLite; one that was set up with PostgreSQL keeps it (its
    records are in it), so there only installing psycopg helps."""
    try:
        import psycopg
    except ImportError:
        if _is_set_up(settings):
            advice = ("Run: %s   This project was set up with PostgreSQL, and its records are "
                      "in it." % util.INSTALL_PY)
        else:
            advice = ("Run: %s   Or use the engine that needs nothing: %s setup --engine sqlite"
                      % (util.INSTALL_PY, util.LAUNCHER))
        raise PclError("The PostgreSQL engine needs the Python package psycopg, which is not "
                       "installed. " + advice, code="psycopg_missing", status=503) from None
    return psycopg


def _identity(text):
    return text


def _out_timestamptz(value):
    return util.utc_text(value)


def _out_timestamp(value):
    return util.utc_text(value.replace(tzinfo=util.UTC))


def _out_date(value):
    return value.isoformat()


def _out_numeric(value):
    return decimal_number(value) if isinstance(value, decimal.Decimal) else value


_OUT = {_TIMESTAMPTZ: _out_timestamptz, _TIMESTAMP: _out_timestamp, _DATE: _out_date,
        _NUMERIC: _out_numeric}


def _error_text(error):
    """The server's own message for a statement that failed (no secrets are in it)."""
    diag = getattr(error, "diag", None)
    primary = getattr(diag, "message_primary", None)
    if primary:
        detail = getattr(diag, "message_detail", None)
        return "%s (%s)" % (primary, detail) if detail else primary
    return str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__


class PostgresDb(Db):
    engine = "postgresql"

    def __init__(self, connection, psycopg):
        super().__init__()
        self._connection = connection
        self._psycopg = psycopg
        from psycopg.types.json import Jsonb
        self._jsonb = Jsonb

    def _run(self, text, values):
        return self._connection.execute(text, values)

    def _run_many(self, text, rows):
        cursor = self._connection.cursor()
        cursor.executemany(text, rows)
        return cursor.rowcount

    def _rows(self, cursor):
        if cursor.description is None:
            return []
        names = [column.name for column in cursor.description]
        converters = [_OUT.get(column.type_code) for column in cursor.description]
        if not any(converters):
            return [dict(zip(names, raw)) for raw in cursor.fetchall()]
        rows = []
        for raw in cursor.fetchall():
            row = {}
            for name, convert, value in zip(names, converters, raw):
                row[name] = value if convert is None or value is None else convert(value)
            rows.append(row)
        return rows

    def _encode(self, kind, clean):
        if kind == "utc":
            return util.parse_utc(clean)                 # an aware datetime -> timestamptz
        if kind == "json":
            return self._jsonb(clean, dumps=_identity)   # the canonical text, sent as jsonb
        return clean                                     # a list of str goes in as text[]

    def _translate(self, error):
        return translate_error(self._psycopg, error)


def translate_error(psycopg, error):
    """The engine-neutral exception for a psycopg error; None for anything else."""
    if isinstance(error, psycopg.IntegrityError):
        return IntegrityError(_error_text(error),
                              _CONSTRAINT_KINDS.get(getattr(error, "sqlstate", None), "other"))
    if isinstance(error, psycopg.OperationalError):
        state = getattr(error, "sqlstate", None) or ""
        if not state or state.startswith(("08", "57P")):
            return Unavailable("The PostgreSQL database stopped answering in the middle of the "
                               "work. Check that it is running (pcldb db status) and try again.",
                               code="db_down")
        return DatabaseError(_error_text(error))
    if isinstance(error, psycopg.Error):
        return DatabaseError(_error_text(error))
    return None


class PostgresStore(Store):
    engine = "postgresql"

    def __init__(self, settings):
        super().__init__(settings)
        self.mode = settings.database.postgresql.mode
        self.hardening_notes = []       # what pg_hardening.apply reported at the last initialize()
        self._ready = False
        self._version = None

    # -- where and as whom ---------------------------------------------------------------------

    def _external_parts(self, role):
        """The parsed connection string of an external role, checked. Holds the password."""
        psycopg = load_psycopg(self.settings)
        from psycopg.conninfo import conninfo_to_dict
        name = "database.postgresql.external.%s (or the environment variable %s)" % (
            role, _SETTING_NAMES[role])
        text = getattr(self.settings.database.postgresql.external, role)
        if not text:
            raise PclError(
                "The setting database.postgresql.mode is \"external\", so pcldb needs the "
                "connection string %s." % name, code="bad_setting")
        try:
            parts = conninfo_to_dict(text)
        except psycopg.Error:
            # The driver's message would repeat the whole string, password included.
            raise PclError(
                "The connection string %s is not written correctly. It looks like: "
                "host=db.example.org port=5432 dbname=pcl user=%s password=..."
                % (name, ROLES[role]), code="bad_setting") from None
        missing = [key for key in ("host", "dbname", "user") if not parts.get(key)]
        if missing:
            raise PclError("The connection string %s must name %s."
                           % (name, " and ".join(missing)), code="bad_setting")
        parts.setdefault("port", "5432")
        return parts

    def conninfo(self, role):
        """The connection string for "owner", "app" or "reader". It holds the password: never
        print or log it. Host, port, database and user are always in it, so no PG* variable of
        the environment can change where or as whom the connection goes."""
        if role not in ROLES:
            raise ValueError("unknown role %r (known: owner, app, reader)" % (role,))
        load_psycopg(self.settings)
        from psycopg.conninfo import make_conninfo
        extra = {"connect_timeout": CONNECT_TIMEOUT_SECONDS,
                 "application_name": "pcldb-%s" % role, "client_encoding": "utf8"}
        if self.mode == "local":
            passwords = pgcluster.read_local_secrets(self.settings)
            postgresql = self.settings.database.postgresql
            return make_conninfo("", host="127.0.0.1", port=postgresql.port,
                                 dbname=postgresql.name, user=ROLES[role],
                                 password=passwords[role], **extra)
        parts = self._external_parts(role)
        parts.update(extra)
        return make_conninfo("", **parts)

    @property
    def database_name(self):
        if self.mode == "local":
            return self.settings.database.postgresql.name
        return self._external_parts("app")["dbname"]

    def _place(self, role="app"):
        """(host, port, database): where a role's connection goes. No secrets."""
        if self.mode == "local":
            postgresql = self.settings.database.postgresql
            return "127.0.0.1", str(postgresql.port), postgresql.name
        parts = self._external_parts(role)
        return parts["host"], str(parts["port"]), parts["dbname"]

    def describe(self):
        try:
            host, port, database = self._place()
        except PclError:        # settings that cannot be used: say so, do not fail here
            return {"engine": "postgresql", "version": "unknown",
                    "where": "unknown (the connection settings are not usable)"}
        if self.mode == "local":
            where = "this computer, port %s" % port
        else:
            where = "%s, port %s, database %s" % (host, port, database)
        return {"engine": "postgresql", "version": self._server_version(), "where": where}

    def _server_version(self):
        if self._version is None:
            try:
                connection = self._connect("app")
            except PclError:
                return "unknown"
            try:
                number = connection.info.server_version
            finally:
                connection.close()
            self._version = "%d.%d" % (number // 10000, number % 10000)
        return self._version

    # -- connecting ----------------------------------------------------------------------------

    def _connect(self, role, *, dbname=None, autocommit=False):
        psycopg = load_psycopg(self.settings)
        from psycopg.conninfo import make_conninfo
        info = self.conninfo(role)
        if dbname is not None:
            info = make_conninfo(info, dbname=dbname)
        try:
            return psycopg.connect(info, autocommit=autocommit, prepare_threshold=None)
        except psycopg.Error as error:
            raise self._connect_error(role, error, dbname) from None

    def _connect_error(self, role, error, dbname=None):
        """A plain-word PclError for a connection that was not made. The driver's own text is
        only looked at, never passed on: it could repeat a connection string."""
        text = str(error)
        host, port, database = self._place(role)
        database = dbname or database
        local = self.mode == "local"
        if "authentication failed" in text or _NO_SUCH_ROLE.search(text):
            if local:
                hint = ("Run: pcldb setup   It sets the roles and their passwords again; your "
                        "records stay.")
            else:
                hint = ("Check the user name and the password in the connection string "
                        "database.postgresql.external.%s (or %s)." % (role, _SETTING_NAMES[role]))
            who = ROLES[role] if local else self._external_parts(role)["user"]
            return Unavailable("PostgreSQL refused the sign-in of the role %s. %s" % (who, hint),
                               code="db_signin_refused")
        if _NO_SUCH_DATABASE.search(text):
            return Unavailable("The PostgreSQL database \"%s\" does not exist yet. Run: pcldb setup"
                               % database, code="db_missing")
        if local and util.port_answers(int(port)):
            return Unavailable(
                "The local PostgreSQL database (127.0.0.1, port %s) answered but did not let "
                "pcldb in: it may still be starting, or too many connections are open. Try "
                "again in a moment." % port, code="db_down")
        if local:
            return Unavailable(
                "The local PostgreSQL database is not running (nothing answered on 127.0.0.1, "
                "port %s). Start it with: pcldb db start   (pcldb serve starts it by itself.)"
                % port, code="db_down")
        return Unavailable(
            "The PostgreSQL server at %s, port %s did not answer or refused the connection. "
            "Check that it is running and that the connection string "
            "database.postgresql.external.%s is right." % (host, port, role), code="db_down")

    @contextlib.contextmanager
    def app(self):
        psycopg = load_psycopg(self.settings)
        with one_block_at_a_time("postgresql %s %s %s" % self._place()):
            connection = self._connect("app")
            db = None
            try:
                if not self._ready:
                    self._require_initialized(connection)
                db = PostgresDb(connection, psycopg)
                try:
                    yield db
                except BaseException:
                    self._rollback(connection)
                    raise
                if db._failed:
                    self._rollback(connection)
                    raise DatabaseError("The transaction was rolled back because one of its "
                                        "statements failed.")
                try:
                    connection.commit()
                except psycopg.Error as error:
                    self._rollback(connection)
                    raise (translate_error(psycopg, error) or error) from None
            finally:
                if db is not None:
                    db._closed = True
                connection.close()

    @staticmethod
    def _rollback(connection):
        try:
            connection.rollback()
        except Exception:
            pass    # the connection is closed right after; the server drops the transaction

    # -- the schema version ----------------------------------------------------------------

    @staticmethod
    def _versions(connection):
        """{"pcl": ..., "app": ...}: None when the schema is not there, "unknown" when it is
        there without a readable version, else the schema_version text. Asks the system
        catalogue first, so no statement fails (a failure would end the transaction)."""
        found = {}
        for schema in ("pcl", "app"):
            exists = connection.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema]).fetchone()
            if not exists:
                found[schema] = None
                continue
            # CASE fixes the order: the later checks would fail on a missing table.
            readable = connection.execute(
                "SELECT CASE WHEN to_regclass(%s) IS NULL THEN FALSE"
                " WHEN NOT has_schema_privilege(%s, 'USAGE') THEN FALSE"
                " ELSE has_table_privilege(%s, 'SELECT') END",
                [schema + ".meta", schema, schema + ".meta"]).fetchone()
            row = None
            if readable and readable[0]:
                row = connection.execute(
                    "SELECT value FROM " + schema + ".meta WHERE key = 'schema_version'"
                ).fetchone()
            found[schema] = str(row[0]) if row and row[0] is not None else "unknown"
        return found

    def _require_initialized(self, connection):
        wanted = str(schema_def.SCHEMA_VERSION)
        versions = self._versions(connection)
        connection.rollback()       # the check must not be part of the caller's transaction
        for found in versions.values():
            if found != wanted:
                raise not_initialized_error(found)
        self._ready = True

    def is_initialized(self):
        """False for a database, schema or role that is not there yet. A server that does not
        answer is not "false" but an Unavailable error: nobody can tell then."""
        if self.mode == "local":
            cluster = pgcluster.LocalCluster(self.settings)
            if not cluster.is_initialized() or not self.settings.local_secrets_path.is_file():
                return False
        try:
            connection = self._connect("app")
        except Unavailable as error:
            if error.code in ("db_missing", "db_signin_refused"):
                return False
            raise
        try:
            versions = self._versions(connection)
        finally:
            connection.close()
        wanted = str(schema_def.SCHEMA_VERSION)
        return all(found == wanted for found in versions.values())

    # -- setting up ---------------------------------------------------------------------------

    def initialize(self):
        """Create what is missing: (local mode) the cluster, the roles and the database; then
        the schemas, tables, views, comments and grants; then the hardening of pg_hardening.
        Safe to call again: an initialised store only gets its grants applied once more.
        In local mode the server is started when needed and is left running."""
        load_psycopg(self.settings)
        if self.mode == "local":
            cluster = pgcluster.LocalCluster(self.settings)
            cluster.init()
            cluster.ensure_running()
            self._ensure_local_roles_and_database()
        else:
            self._check_external_roles()
            self._ensure_external_database()
        connection = self._connect("owner")
        try:
            self._build(connection)
            connection.commit()
        except BaseException:
            self._rollback(connection)
            raise
        finally:
            connection.close()
        self._ready = True

    def _role_statement(self, connection, name, password):
        """CREATE or ALTER ROLE with the password already hashed, so the clear password is
        never in a statement (statements can end up in the server's log)."""
        from psycopg import sql
        verifier = connection.pgconn.encrypt_password(
            password.encode("utf-8"), name.encode("utf-8"), b"scram-sha-256").decode("ascii")
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [name]).fetchone()
        verb = "ALTER" if exists else "CREATE"
        return sql.SQL(
            verb + " ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
            "NOREPLICATION NOBYPASSRLS PASSWORD {}").format(sql.Identifier(name),
                                                            sql.Literal(verifier))

    def _create_database(self, connection, name):
        """CREATE DATABASE (when missing) on an autocommit connection. The locale comes from
        template0, which is what initdb was told; text columns are COLLATE "C" anyway."""
        psycopg = load_psycopg(self.settings)
        from psycopg import sql
        if connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone():
            return False
        try:
            connection.execute(sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                    sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            return False        # another program made it in the same moment
        except psycopg.Error as error:
            raise PclError("PostgreSQL could not create the database \"%s\": %s"
                           % (name, _error_text(error)), code="db_setup_failed",
                           status=503) from None
        return True

    def _ensure_local_roles_and_database(self):
        psycopg = load_psycopg(self.settings)
        passwords = pgcluster.read_local_secrets(self.settings)
        connection = self._connect("owner", dbname="postgres", autocommit=True)
        try:
            for role in ("app", "reader"):
                try:
                    connection.execute(
                        self._role_statement(connection, ROLES[role], passwords[role]))
                except psycopg.errors.DuplicateObject:
                    pass        # another program made the role in the same moment
            self._create_database(connection, self.database_name)
        except psycopg.Error as error:
            raise PclError("PostgreSQL could not set up its roles: %s" % _error_text(error),
                           code="db_setup_failed", status=503) from None
        finally:
            connection.close()

    def _check_external_roles(self):
        for role in ("app", "reader"):
            user = self._external_parts(role)["user"]
            if user != ROLES[role]:
                raise PclError(
                    "The connection string database.postgresql.external.%s signs in as \"%s\"; "
                    "it must sign in as the database role %s, because the rights are given to "
                    "that role." % (role, user, ROLES[role]), code="bad_setting")

    def _ensure_external_database(self):
        psycopg = load_psycopg(self.settings)
        try:
            connection = self._connect("owner", autocommit=True)
        except Unavailable as error:
            if error.code != "db_missing":
                raise
            # The server is there, the database is not: make it from the maintenance database.
            connection = self._connect("owner", dbname="postgres", autocommit=True)
            try:
                self._create_database(connection, self._external_parts("owner")["dbname"])
            finally:
                connection.close()
            connection = self._connect("owner", autocommit=True)
        try:
            found = {row[0] for row in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                [[APP_ROLE, READER_ROLE]]).fetchall()}
        except psycopg.Error as error:
            raise PclError("PostgreSQL could not be asked for its roles: %s" % _error_text(error),
                           code="db_setup_failed", status=503) from None
        finally:
            connection.close()
        missing = [name for name in (APP_ROLE, READER_ROLE) if name not in found]
        if missing:
            raise PclError(
                "The PostgreSQL server has no role %s. Ask its administrator to create the roles "
                "pcl_app and pcl_reader (LOGIN, with a password, no other rights); pcldb then "
                "gives them their rights itself." % " and no role ".join(missing),
                code="db_setup_failed", status=503)

    def _build(self, connection):
        """Inside one transaction as the owner: the missing schemas, then grants and hardening."""
        psycopg = load_psycopg(self.settings)
        from psycopg import sql
        wanted = str(schema_def.SCHEMA_VERSION)
        try:
            # One setup at a time: a second program waits here, then finds everything made.
            # (An owner that may not take the lock simply goes on without it.)
            try:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('pcldb initialize'))")
            except psycopg.Error:
                pass
            versions = self._versions(connection)
            for found in versions.values():
                if found not in (None, wanted):
                    raise not_initialized_error(found)
            for schema in ("pcl", "app"):
                if versions[schema] is not None:
                    continue
                comments = []
                for statement in schema_def.ddl("postgresql", schema):
                    if statement.startswith("COMMENT ON "):
                        comments.append(statement)
                    else:
                        connection.execute(statement)
                if comments:
                    connection.execute(";\n".join(comments))    # one round trip for them all
                connection.execute(
                    "INSERT INTO " + schema + ".meta (key, value) VALUES ('schema_version', %s)",
                    [wanted])
            database = sql.Identifier(self.database_name)
            app = sql.Identifier(APP_ROLE)
            grants = [sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, app),
                      sql.SQL("GRANT USAGE ON SCHEMA pcl, app TO {}").format(app)]
            for item in schema_def.TABLES:
                grants.append(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                    sql.Identifier(item.schema, item.name), app))
            for item in schema_def.VIEWS:
                grants.append(sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(item.schema, item.name), app))
            connection.execute(sql.SQL(";\n").join(grants))
            notes = pg_hardening.apply(connection, self.database_name)
            self.hardening_notes = [str(note) for note in notes or ()]
        except psycopg.Error as error:
            raise PclError("PostgreSQL could not set up the database: %s" % _error_text(error),
                           code="db_setup_failed", status=503) from None

    def destroy(self):
        """Drop the schemas pcl and app with everything in them. The database, the roles and
        (local mode) the cluster stay."""
        psycopg = load_psycopg(self.settings)
        self._ready = False
        if self.mode == "local":
            cluster = pgcluster.LocalCluster(self.settings)
            if not cluster.is_initialized():
                return
            cluster.ensure_running()
        try:
            connection = self._connect("owner")
        except Unavailable as error:
            if error.code == "db_missing":
                return
            raise
        try:
            connection.execute("DROP SCHEMA IF EXISTS pcl CASCADE")
            connection.execute("DROP SCHEMA IF EXISTS app CASCADE")
            connection.commit()
        except psycopg.Error as error:
            self._rollback(connection)
            raise PclError("PostgreSQL could not remove the old tables: %s" % _error_text(error),
                           code="db_setup_failed", status=503) from None
        finally:
            connection.close()
