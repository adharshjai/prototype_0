"""What the read-only role may do in PostgreSQL (specification section 9).

`apply(conn, database_name)` is called by `PostgresStore.initialize()` every time it runs, after
the tables, views and the grants of `pcl_app` exist. It is safe to run again and again.

    conn            a psycopg connection as the owner, connected to `database_name`, inside an
                    open transaction that initialize() commits. Nothing here commits, rolls
                    back or closes it; every step runs in its own savepoint.
    database_name   the database's name.

Expert mode lets a person type SQL. That SQL runs as the login role `pcl_reader`, and this
module decides what that role is able to do. Three groups of steps:

1. The rights (the owner of the database can always do these): take away what every role gets
   through PUBLIC (connecting, temporary tables, the schema `public`), start `pcl_reader` from
   nothing, then give it exactly: connect, see the schema `pcl`, read its tables and views.
2. What needs a superuser (the local cluster's owner is one; an external database's owner may
   not be): no procedural language for PUBLIC (no DO blocks, no CREATE FUNCTION), no EXECUTE for
   PUBLIC on the built-in functions that change settings inside a statement, run SQL given as
   text, sleep, signal or watch other sessions, take advisory locks, send notifications or
   write large objects; and the role's own limits (read-only by default, time limits, a cap on
   temporary files, a connection limit, statement logging).
3. A check of the result (`verify`). If the reader could read the private schema `app` or
   change anything, `apply` stops with a PclError: such a database must not be used. What only
   makes the sandbox weaker than it could be is returned as notes (and `check(store)` gives
   the same list at any time, asked as the reader itself).

The steps of group 2 that this owner may not do are skipped and reported. Expert mode then
still rests on the rights of group 1, the read-only transaction, the one-statement protocol
and the time limits that reader_postgres.py sends with every connection.

Supported: PostgreSQL 16 to 19.
"""

from __future__ import annotations

from ..errors import PclError

READER_ROLE = "pcl_reader"
APP_ROLE = "pcl_app"

# Role settings (a floor; reader_postgres.py repeats the user-settable ones in the options of
# every connection, where a person's SQL cannot erase them). (name, value, needs a superuser,
# first server version that knows it)
ROLE_SETTINGS = (
    ("default_transaction_read_only", "on", False, 0),
    ("statement_timeout", "30s", False, 0),
    ("lock_timeout", "3s", False, 0),
    ("idle_in_transaction_session_timeout", "30s", False, 0),
    ("idle_session_timeout", "60s", False, 0),
    ("transaction_timeout", "60s", False, 170000),
    ("work_mem", "16MB", False, 0),
    ("max_parallel_workers_per_gather", "0", False, 0),
    ("jit", "off", False, 0),
    ("search_path", "pcl", False, 0),
    ("temp_file_limit", "256MB", True, 0),     # only a superuser can set or change it
    ("log_statement", "all", True, 0),         # the server's own record of every reader statement
)

CONNECTION_LIMIT = 6

# Built-in functions PUBLIC may call in a stock database and the reader must not. Matched by
# name so that every overload of every supported version is covered.
BLOCKED_FUNCTIONS = (
    "set_config",                                   # changes a setting inside a statement
    "pg_notify",
    "pg_cancel_backend", "pg_terminate_backend",    # allowed against sessions of the same role
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "ts_stat",                                      # runs SQL text given as an argument
    "pg_logical_emit_message",                      # writes to the log of changes, even read-only
    "pg_export_snapshot",                           # writes a file on the server per call
    "pg_lock_status",                               # pg_locks: what other sessions hold
    "lo_creat", "lo_create", "lo_from_bytea", "lo_put", "lo_open", "lowrite", "lo_truncate",
    "lo_truncate64", "lo_unlink",
    # Not callable by PUBLIC in a stock database; listed so that the check notices a grant.
    "lo_import", "lo_export", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
    "pg_stat_file", "pg_reload_conf", "pg_rotate_logfile",
)
BLOCKED_PATTERNS = (
    r"^pg_(try_)?advisory_(xact_)?(un)?lock",       # every advisory lock function
    r"^(query|table|cursor|schema|database)_to_xml",    # run SQL text / dump whole schemas
    r"^pg_stat_get_",                               # other sessions' SQL text; activity counters
    r"^dblink",                                     # only there when someone installed it
)
# The listed functions that PUBLIC or the reader can call now: (signature, PUBLIC can).
_BLOCKED_SQL = """
SELECT p.oid::regprocedure::text,
       pg_catalog.has_function_privilege('public', p.oid, 'EXECUTE')
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname IN ('pg_catalog', 'public')
  AND (p.proname = ANY(%(names)s)
       OR p.proname ~ ANY(%(patterns)s)
       OR (p.proname = 'ts_rewrite' AND p.pronargs = 2))
  AND (pg_catalog.has_function_privilege('public', p.oid, 'EXECUTE')
       OR pg_catalog.has_function_privilege(%(reader)s, p.oid, 'EXECUTE'))
ORDER BY 1
"""


class Result(list):
    """What `apply` did: a list of notes (plain sentences about steps that were skipped or
    things that stay weaker than they could be), with `applied` (how many steps ran)."""

    applied = 0


def _run(conn, statement, **names):
    from psycopg import sql
    parts = {key: sql.Identifier(value) for key, value in names.items()}
    conn.execute(sql.SQL(statement).format(**parts))


def _step(conn, result, note, statements, **names):
    """Run statements in a savepoint. When the database refuses, undo just them, remember the
    note, and go on. Returns True when they ran."""
    import psycopg
    try:
        with conn.transaction():
            # One round trip for the whole step (there are hundreds of REVOKEs).
            _run(conn, ";\n".join(statements), **names)
    except psycopg.Error as error:
        result.append("%s (PostgreSQL said: %s)" % (note, _first_line(error)))
        return False
    result.applied += 1
    return True


def _first_line(error):
    diag = getattr(error, "diag", None)
    text = getattr(diag, "message_primary", None) or str(error)
    return text.strip().splitlines()[0] if text.strip() else type(error).__name__


def _scalar(conn, statement, params=None):
    row = conn.execute(statement, params).fetchone()
    return row[0] if row else None


def apply(conn, database_name):
    """Give `pcl_reader` exactly its rights and limits in this database. See the module text.

    Returns a Result (a list of notes). Raises PclError when the reader could still read the
    private schema or change something.
    """
    result = Result()
    names = {"database": database_name, "reader": READER_ROLE, "app": APP_ROLE}
    owner = _scalar(conn, "SELECT current_user")
    names["owner"] = owner
    superuser = bool(_scalar(conn, "SELECT rolsuper FROM pg_catalog.pg_roles "
                                   "WHERE rolname = current_user"))
    version = conn.info.server_version
    not_owner = "This database's owner role could not %s; ask the server's administrator"

    # ---- 1. the rights -------------------------------------------------------------------
    _step(conn, result, not_owner % "take the default rights on the database from PUBLIC",
          ["REVOKE ALL ON DATABASE {database} FROM PUBLIC",
           "REVOKE ALL ON DATABASE {database} FROM {reader}",
           "GRANT CONNECT ON DATABASE {database} TO {reader}"], **names)
    if _scalar(conn, "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = 'public'"):
        _step(conn, result, not_owner % "close the schema public",
              ["REVOKE ALL ON SCHEMA public FROM PUBLIC",
               "REVOKE ALL ON SCHEMA public FROM {reader}"], **names)
    _step(conn, result, not_owner % "set the rights on the schemas pcl and app",
          ["REVOKE ALL ON SCHEMA pcl, app FROM PUBLIC",
           "REVOKE ALL ON SCHEMA pcl, app FROM {reader}",
           "REVOKE ALL ON ALL TABLES IN SCHEMA pcl, app FROM PUBLIC",
           "REVOKE ALL ON ALL TABLES IN SCHEMA pcl, app FROM {reader}",
           "REVOKE ALL ON ALL SEQUENCES IN SCHEMA pcl, app FROM PUBLIC",
           "REVOKE ALL ON ALL SEQUENCES IN SCHEMA pcl, app FROM {reader}",
           "REVOKE ALL ON ALL ROUTINES IN SCHEMA pcl, app FROM PUBLIC",
           "REVOKE ALL ON ALL ROUTINES IN SCHEMA pcl, app FROM {reader}",
           "GRANT USAGE ON SCHEMA pcl TO {reader}",
           "GRANT SELECT ON ALL TABLES IN SCHEMA pcl TO {reader}"], **names)
    # Functions the owner makes later are not callable by everyone. (New tables in pcl are
    # NOT readable by themselves: initialize() grants them when it runs again.)
    _step(conn, result, not_owner % "change its default rights for new functions",
          ["ALTER DEFAULT PRIVILEGES FOR ROLE {owner} REVOKE EXECUTE ON ROUTINES FROM PUBLIC"],
          **names)

    # ---- 2. what needs a superuser -------------------------------------------------------
    needs_superuser = ("The database owner is not a superuser, so it could not %s. Ask the "
                       "server's administrator to run pcldb setup once as a superuser, or "
                       "accept the weaker sandbox")
    languages = [row[0] for row in conn.execute(
        "SELECT lanname FROM pg_catalog.pg_language WHERE lanname IN ('plpgsql', 'sql') "
        "ORDER BY 1").fetchall()]
    if languages:
        statements = []
        for language in languages:
            statements.append("REVOKE ALL ON LANGUAGE %s FROM PUBLIC" % language)
            statements.append("REVOKE ALL ON LANGUAGE %s FROM {reader}" % language)
            statements.append("GRANT USAGE ON LANGUAGE %s TO {owner}" % language)
        _step(conn, result, needs_superuser % "take the procedural languages from PUBLIC",
              statements, **names)

    blocked = conn.execute(
        _BLOCKED_SQL, {"names": list(BLOCKED_FUNCTIONS), "patterns": list(BLOCKED_PATTERNS),
                       "reader": READER_ROLE}).fetchall()
    if blocked:
        monitor = bool(_scalar(conn, "SELECT 1 FROM pg_catalog.pg_roles "
                                     "WHERE rolname = 'pg_monitor'"))
        keep = "{owner}, {app}" + (", pg_monitor" if monitor else "")
        statements = []
        for routine, public in blocked:     # regprocedure text: already quoted by the server
            statements.append("REVOKE ALL ON ROUTINE %s FROM {reader}" % _braces(routine))
            if public:
                # Whoever could call it through PUBLIC and still needs it keeps it by name.
                # (Never GRANT a function PUBLIC did not have: lo_import reads server files.)
                statements.append("REVOKE ALL ON ROUTINE %s FROM PUBLIC" % _braces(routine))
                statements.append("GRANT EXECUTE ON ROUTINE %s TO %s" % (_braces(routine), keep))
        if superuser:
            _step(conn, result, needs_superuser % "take %d built-in functions from PUBLIC"
                  % len(blocked), statements, **names)
        else:
            result.append((needs_superuser % "take %d built-in functions (set_config, "
                           "pg_sleep, advisory locks, pg_stat_get_activity ...) from PUBLIC"
                           % len(blocked)) + ".")

    for name, value, superuser_only, since in ROLE_SETTINGS:
        if version < since:
            continue
        note = (needs_superuser if superuser_only else not_owner) % (
            "set the reader's limit %s" % name)
        _step(conn, result, note,
              ["ALTER ROLE {reader} IN DATABASE {database} SET %s = '%s'" % (name, value)],
              **names)
    _step(conn, result, not_owner % "limit the reader's connections",
          ["ALTER ROLE {reader} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
           "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT %d" % CONNECTION_LIMIT], **names)

    # A cluster that pcldb made for itself (its first superuser is the owner role): nobody
    # needs the two stock databases, and the function revokes above do not reach into them.
    if superuser and _scalar(conn, "SELECT oid = 10 FROM pg_catalog.pg_roles "
                                   "WHERE rolname = current_user"):
        for other in ("postgres", "template1"):
            if other != database_name and _scalar(
                    conn, "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", [other]):
                _step(conn, result, "The database %s could not be closed to other roles" % other,
                      ["REVOKE CONNECT ON DATABASE {other} FROM PUBLIC"], other=other)

    # ---- 3. the check ---------------------------------------------------------------------
    fatal, notes = verify(conn, database_name)
    if fatal:
        raise PclError(
            "The PostgreSQL role pcl_reader has rights that expert mode must not have: %s. "
            "pcldb did not finish setting up this database. Ask the server's administrator "
            "to take these rights away, then run pcldb setup again." % "; ".join(fatal),
            code="db_setup_failed", status=503, details={"problems": fatal})
    for note in notes:
        if note not in result:
            result.append(note)
    return result


def _braces(text):
    """Text that goes through psycopg.sql.SQL(...).format: its braces are literal."""
    return text.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------- the check

_ATTRIBUTES = """
SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolconnlimit
FROM pg_catalog.pg_roles WHERE rolname = %s
"""

_MEMBERSHIPS = """
SELECT r.rolname, m.rolname
FROM pg_catalog.pg_auth_members a
JOIN pg_catalog.pg_roles r ON r.oid = a.roleid
JOIN pg_catalog.pg_roles m ON m.oid = a.member
WHERE r.rolname = %(reader)s OR m.rolname = %(reader)s
"""

_OWNED = """
SELECT 'the ' || CASE c.relkind WHEN 'S' THEN 'sequence' WHEN 'v' THEN 'view' ELSE 'table' END
       || ' ' || n.nspname || '.' || c.relname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_roles o ON o.oid = c.relowner
WHERE o.rolname = %(reader)s
UNION ALL
SELECT 'the schema ' || n.nspname
FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles o ON o.oid = n.nspowner
WHERE o.rolname = %(reader)s
UNION ALL
SELECT 'the function ' || p.proname
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_roles o ON o.oid = p.proowner
WHERE o.rolname = %(reader)s
LIMIT 20
"""

# Everything outside the system schemas and pcl that the reader can read, and everything at all
# that it can change. (UPDATE on pg_settings is granted to PUBLIC by PostgreSQL itself; it only
# works through set_config's rules and is refused in a read-only transaction.)
_READABLE = """
SELECT n.nspname || '.' || c.relname
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pcl')
  AND n.nspname !~ '^pg_(toast|temp)'
  AND pg_catalog.has_schema_privilege(%(reader)s, n.oid, 'USAGE')
  AND pg_catalog.has_table_privilege(%(reader)s, c.oid, 'SELECT')
ORDER BY 1 LIMIT 20
"""

_WRITABLE = """
SELECT n.nspname || '.' || c.relname
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p')
  AND NOT (n.nspname = 'pg_catalog' AND c.relname = 'pg_settings')
  AND pg_catalog.has_table_privilege(
        %(reader)s, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
ORDER BY 1 LIMIT 20
"""

_SEQUENCES = """
SELECT n.nspname || '.' || c.relname
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S'
  AND CASE WHEN c.relkind = 'S'
           THEN pg_catalog.has_sequence_privilege(%(reader)s, c.oid, 'USAGE, SELECT, UPDATE')
      END
ORDER BY 1 LIMIT 20
"""

# Views of pcl that read something outside pcl with their owner's rights: a window into data
# the reader has no right to.
_VIEW_WINDOWS = """
SELECT DISTINCT v.relname || ' reads ' || tn.nspname || '.' || t.relname
FROM pg_catalog.pg_class v
JOIN pg_catalog.pg_namespace vn ON vn.oid = v.relnamespace
JOIN pg_catalog.pg_rewrite r ON r.ev_class = v.oid
JOIN pg_catalog.pg_depend d ON d.classid = 'pg_catalog.pg_rewrite'::regclass
                           AND d.objid = r.oid
                           AND d.refclassid = 'pg_catalog.pg_class'::regclass
JOIN pg_catalog.pg_class t ON t.oid = d.refobjid
JOIN pg_catalog.pg_namespace tn ON tn.oid = t.relnamespace
WHERE vn.nspname = 'pcl' AND v.relkind IN ('v', 'm') AND t.oid <> v.oid
  AND tn.nspname <> 'pcl'
ORDER BY 1 LIMIT 20
"""

_CALLABLE = """
SELECT p.proname, count(*)
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname IN ('pg_catalog', 'public')
  AND (p.proname = ANY(%(names)s)
       OR p.proname ~ ANY(%(patterns)s)
       OR (p.proname = 'ts_rewrite' AND p.pronargs = 2))
  AND pg_catalog.has_function_privilege(%(reader)s, p.oid, 'EXECUTE')
GROUP BY 1 ORDER BY 1
"""

_DEFINER = """
SELECT n.nspname || '.' || p.proname
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef AND pg_catalog.has_function_privilege(%(reader)s, p.oid, 'EXECUTE')
ORDER BY 1 LIMIT 20
"""

_SETTINGS = """
SELECT s.setconfig
FROM pg_catalog.pg_db_role_setting s
JOIN pg_catalog.pg_roles r ON r.oid = s.setrole
LEFT JOIN pg_catalog.pg_database d ON d.oid = s.setdatabase
WHERE r.rolname = %(reader)s AND (s.setdatabase = 0 OR d.datname = %(database)s)
"""


def verify(conn, database_name):
    """What `pcl_reader` can do in this database, checked from the system catalogue.

    Works on a connection of any role (the owner inside `apply`, or the reader itself).
    Returns (fatal, notes): two lists of plain sentences. `fatal`: the reader could read the
    private data or change something; expert mode must not run. `notes`: the sandbox is weaker
    than it could be (a step of the hardening needs a superuser, or was undone).
    """
    reader = READER_ROLE
    params = {"reader": reader, "database": database_name,
              "names": list(BLOCKED_FUNCTIONS), "patterns": list(BLOCKED_PATTERNS)}
    fatal, notes = [], []

    def rows(statement):
        return conn.execute(statement, params).fetchall()

    row = conn.execute(_ATTRIBUTES, [reader]).fetchone()
    if row is None:
        return ["the role pcl_reader does not exist"], notes
    for flag, name in zip(row[:5], ("superuser", "may create databases", "may create roles",
                                    "replication", "bypasses row security")):
        if flag:
            fatal.append("it has the attribute \"%s\"" % name)
    if row[5] is None or row[5] < 0:
        notes.append("The reader role has no connection limit.")
    for group, member in rows(_MEMBERSHIPS):
        fatal.append("%s is a member of %s" % (member, group))
    owned = [item[0] for item in rows(_OWNED)]
    if owned:
        fatal.append("it owns %s" % ", ".join(owned))

    def has(kind, *arguments):
        return bool(_scalar(conn, "SELECT pg_catalog.has_%s_privilege(%s)"
                            % (kind, ", ".join(["%s"] * len(arguments))), list(arguments)))

    if not has("database", reader, database_name, "CONNECT"):
        notes.append("The reader role may not connect to the database; expert mode cannot run.")
    if has("database", reader, database_name, "TEMPORARY"):
        fatal.append("it may create temporary tables")
    if has("database", reader, database_name, "CREATE"):
        fatal.append("it may create schemas")
    schemas = {item[0] for item in conn.execute(
        "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname IN ('pcl', 'app', 'public')")}
    if "app" in schemas and has("schema", reader, "app", "USAGE"):
        fatal.append("it may use the private schema app")
    for schema in ("pcl", "app", "public"):
        if schema in schemas and has("schema", reader, schema, "CREATE"):
            fatal.append("it may create objects in the schema %s" % schema)
    if "public" in schemas and has("schema", reader, "public", "USAGE"):
        notes.append("The reader role may use the schema public.")
    readable = [item[0] for item in rows(_READABLE)]
    if readable:
        fatal.append("it can read %s" % ", ".join(readable))
    writable = [item[0] for item in rows(_WRITABLE)]
    if writable:
        fatal.append("it can change %s" % ", ".join(writable))
    sequences = [item[0] for item in rows(_SEQUENCES)]
    if sequences:
        fatal.append("it can use the sequence %s" % ", ".join(sequences))
    windows = [item[0] for item in rows(_VIEW_WINDOWS)]
    if windows:
        fatal.append("a view of pcl shows data from outside pcl (%s)" % ", ".join(windows))
    definer = [item[0] for item in rows(_DEFINER)]
    if definer:
        fatal.append("it can call a function that runs with its owner's rights (%s)"
                     % ", ".join(definer))

    languages = [item[0] for item in conn.execute(
        "SELECT lanname FROM pg_catalog.pg_language WHERE lanpltrusted "
        "AND pg_catalog.has_language_privilege(%s, oid, 'USAGE') ORDER BY 1", [reader])]
    if languages:
        notes.append("The reader role may use the language %s (DO blocks; they are still "
                     "read-only)." % ", ".join(languages))
    callable_ = rows(_CALLABLE)
    if callable_:
        shown = ", ".join(item[0] for item in callable_[:12])
        more = "" if len(callable_) <= 12 else " and %d more" % (len(callable_) - 12)
        notes.append(
            "The reader role may call these built-in functions: %s%s. With them one expert "
            "user can see or stop another's running query and change time limits inside a "
            "statement; the reader's own deadline still ends every query." % (shown, more))
    settings = {}
    for (config,) in rows(_SETTINGS):
        for item in config or ():
            name, _, value = item.partition("=")
            settings[name] = value
    version = conn.info.server_version
    missing = [name for name, _value, _su, since in ROLE_SETTINGS
               if version >= since and name not in settings]
    if missing:
        notes.append("The reader role has no stored limit for: %s. (Each expert query still "
                     "sends its own time limits.)" % ", ".join(missing))
    for other in ("postgres", "template1"):
        if other != database_name and _scalar(
                conn, "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", [other]) \
                and has("database", reader, other, "CONNECT"):
            notes.append("The reader role may also connect to the database %s." % other)
    if _scalar(conn, "SELECT pg_catalog.current_setting('lo_compat_privileges')") != "off":
        notes.append("The server setting lo_compat_privileges is on; switch it off.")
    return fatal, notes


def check(store):
    """The same check at any time, asked as the reader itself (no owner sign-in needed):
    {"problems": [...], "notes": [...]}. `problems` not empty means expert mode must not run.
    For `pcldb doctor`."""
    import psycopg

    from . import postgres_engine
    try:
        connection = psycopg.connect(store.conninfo("reader"), autocommit=True)
    except psycopg.Error as error:
        raise store._connect_error("reader", error) from None
    try:
        fatal, notes = verify(connection, store.database_name)
    except psycopg.Error as error:
        raise PclError("PostgreSQL could not be asked what the reader role may do: %s"
                       % postgres_engine._error_text(error), code="db_check_failed",
                       status=503) from None
    finally:
        connection.close()
    return {"problems": fatal, "notes": notes}
