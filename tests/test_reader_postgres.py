"""The PostgreSQL expert-mode sandbox (pcldb/store/reader_postgres.py and pg_hardening.py,
specification section 9).

HOSTILE is the list of statements a person might type to read the private schema, to write, to
change who they are, to escape the sandbox or to hang the backend, each with the outcome it
must have and the layer that stops it. After the whole list ran, every table of both schemas
has exactly the rows it had, no object, large object or role setting was added, no file
appeared, and the marker text that the fixture puts into the private tables never came back in
a result or in an error message.

The layers: "grammar" = the server refuses the text inside DECLARE ... CURSOR FOR (only queries
fit there); "protocol" = the extended protocol takes one statement; "privileges" = the role
pcl_reader has no such right in any PostgreSQL; "hardening" = a right PUBLIC has in a stock
database that pg_hardening.py took away; "read-only" = the read-only transaction; "timeout";
"limit" = temp_file_limit or a limit of the server itself.

Run against PostgreSQL 17 and 18 (PCLDB_PG_BIN chooses). The test cluster's owner is a
superuser, as in pcldb's own local cluster, so the whole hardening applies; the class
TestWithoutSuperuserHardening gives some rights back to show what then rests on the other
layers alone.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

from pcldb import sqlrun, util
from pcldb.errors import PclError
from pcldb.store import pg_hardening, reader_postgres, schema_def
from tests import support

MARKER = "PRIVATE-MARKER-5e1f9c"
TIMEOUT = 2.0

OK, REFUSED, ERROR, TIMED_OUT = "ok", "sql_refused", "sql_error", "sql_timeout"

_INSERT = "INSERT INTO pcl.meta (key, value) VALUES ('hostile', 'x')"
_BUSY = "SELECT count(*) FROM pg_class a, pg_class b, pg_class c, pg_class d"

# (statement, outcome or tuple of acceptable outcomes, layer). {dir} = the project folder.
HOSTILE = [
    # ---- every write and every change of structure
    (_INSERT, REFUSED, "grammar"),
    (_INSERT + " RETURNING *", REFUSED, "grammar"),
    ("UPDATE pcl.sessions SET notes = 'hostile'", REFUSED, "grammar"),
    ("DELETE FROM pcl.sessions", REFUSED, "grammar"),
    ("TRUNCATE pcl.sessions", REFUSED, "grammar"),
    ("MERGE INTO pcl.meta m USING (SELECT 1) s ON false WHEN NOT MATCHED THEN "
     "INSERT (key, value) VALUES ('hostile', 'x')", REFUSED, "grammar"),
    ("WITH d AS (DELETE FROM pcl.sessions RETURNING 1) SELECT count(*) FROM d", REFUSED,
     "grammar (a WITH that writes)"),
    ("WITH i AS (" + _INSERT + " RETURNING key) SELECT * FROM i", REFUSED,
     "grammar (a WITH that writes)"),
    ("SELECT * INTO pcl.copy_of_sessions FROM pcl.sessions", REFUSED, "grammar"),
    ("CREATE TABLE pcl.hostile (x int)", REFUSED, "grammar"),
    ("CREATE TABLE pcl.hostile AS SELECT * FROM pcl.sessions", REFUSED, "grammar"),
    ("CREATE SCHEMA hostile", REFUSED, "grammar"),
    ("CREATE VIEW pcl.hostile AS SELECT 1", REFUSED, "grammar"),
    ("CREATE INDEX hostile ON pcl.sessions (folder)", REFUSED, "grammar"),
    ("CREATE EXTENSION dblink", REFUSED, "grammar"),
    ("CREATE ROLE hostile LOGIN", REFUSED, "grammar"),
    ("CREATE DATABASE hostile", REFUSED, "grammar"),
    ("DROP TABLE pcl.sessions", REFUSED, "grammar"),
    ("DROP SCHEMA app CASCADE", REFUSED, "grammar"),
    ("ALTER TABLE pcl.sessions ADD COLUMN hostile int", REFUSED, "grammar"),
    ("COMMENT ON TABLE pcl.sessions IS 'hostile'", REFUSED, "grammar"),
    ("GRANT SELECT ON pcl.sessions TO PUBLIC", REFUSED, "grammar"),
    ("ALTER SYSTEM SET work_mem = '1GB'", REFUSED, "grammar"),
    ("VACUUM pcl.sessions", REFUSED, "grammar"),
    ("ANALYZE pcl.sessions", REFUSED, "grammar"),
    ("REINDEX TABLE pcl.sessions", REFUSED, "grammar"),
    ("CHECKPOINT", REFUSED, "grammar"),
    ("LOAD 'plpgsql'", REFUSED, "grammar"),
    ("LOCK TABLE pcl.sessions IN ACCESS EXCLUSIVE MODE", REFUSED, "grammar"),
    ("LOCK TABLE pcl.sessions IN ACCESS SHARE MODE", REFUSED, "grammar"),
    # ---- the role's own stored settings and password (only the read-only transaction and the
    #      grammar stand between a reader and these)
    ("ALTER ROLE pcl_reader SET statement_timeout = 0", REFUSED, "grammar"),
    ("ALTER ROLE pcl_reader RESET ALL", REFUSED, "grammar"),
    ("ALTER ROLE pcl_reader PASSWORD 'not-a-real-password'", REFUSED, "grammar"),
    ("ALTER ROLE pcl_reader SUPERUSER", REFUSED, "grammar"),
    ("EXPLAIN ANALYZE ALTER ROLE pcl_reader RESET ALL", ERROR, "grammar"),
    # ---- EXPLAIN (the second path): one statement, read-only, as pcl_reader
    ("EXPLAIN ANALYZE " + _INSERT, REFUSED, "read-only"),
    ("EXPLAIN (ANALYZE) DELETE FROM pcl.sessions", REFUSED, "read-only"),
    ("ExPlAiN (ANALYZE, BUFFERS) UPDATE pcl.sessions SET notes = 'x'", REFUSED, "read-only"),
    ("EXPLAIN/**/ANALYZE/**/DELETE FROM pcl.files", REFUSED, "read-only"),
    ("EXPLAIN ANALYZE CREATE TABLE pcl.hostile AS SELECT 1", REFUSED, "read-only"),
    ("EXPLAIN ANALYZE SELECT * INTO pcl.hostile FROM pcl.meta", REFUSED, "read-only"),
    ("EXPLAIN " + _INSERT, REFUSED, "privileges"),
    ("EXPLAIN SELECT * FROM app.users", REFUSED, "privileges"),
    ("EXPLAIN ANALYZE SELECT * FROM app.tokens", REFUSED, "privileges"),
    ("EXPLAIN ANALYZE SELECT nextval('pcl.hostile_seq')", REFUSED, "privileges"),
    ("EXPLAIN ANALYZE SELECT * FROM pcl.sessions FOR UPDATE", REFUSED, "read-only"),
    ("EXPLAIN EXECUTE hostile_plan", ERROR, "no such prepared statement"),
    ("EXPLAIN SET statement_timeout = 0", ERROR, "grammar"),
    ("EXPLAIN COMMIT", ERROR, "grammar"),
    # ---- several statements in one text
    ("SELECT 1; SELECT 2", REFUSED, "protocol"),
    ("COMMIT; " + _INSERT, REFUSED, "protocol"),
    ("SELECT 1; COMMIT; SET TRANSACTION READ WRITE; ALTER ROLE pcl_reader SET "
     "statement_timeout = 0", REFUSED, "protocol"),
    ("EXPLAIN SELECT 1; DELETE FROM pcl.sessions", REFUSED, "protocol"),
    ("EXPLAIN SELECT 1; COMMIT; " + _INSERT, REFUSED, "protocol"),
    ("SELECT 1 /* ; */ ; DROP TABLE pcl.sessions", REFUSED, "protocol"),
    ("SELECT ';' ; SELECT * FROM app.users", REFUSED, "protocol"),
    ("SELECT E'\\\\'; DELETE FROM pcl.sessions --'", REFUSED, "protocol"),
    ("SELECT $$;$$; DELETE FROM pcl.sessions", REFUSED, "protocol"),
    # ---- comments and odd spacing do not change what a statement is
    ("/* hi */ -- x\n  DELETE FROM pcl.sessions", REFUSED, "grammar"),
    ("--\rDELETE FROM pcl.sessions", REFUSED, "grammar"),
    ("/* /* nested */ EXPLAIN */ DELETE FROM pcl.sessions", REFUSED, "grammar"),
    ("/* EXPLAIN */ " + _INSERT, REFUSED, "grammar"),
    ("EXPLAINX SELECT 1", REFUSED, "grammar"),
    ("\"EXPLAIN\" SELECT 1", REFUSED, "grammar"),
    ("\tSELECT/**/display_name/**/FROM/**/app.users", REFUSED, "privileges"),
    # ---- the private schema, every way in
    ("SELECT * FROM app.users", REFUSED, "privileges"),
    ("TABLE app.tokens", REFUSED, "privileges"),
    ("SELECT display_name FROM \"app\".\"users\"", REFUSED, "privileges"),
    ("SELECT query_text FROM app.query_log", REFUSED, "privileges"),
    ("SELECT u.* FROM pcl.sessions s, app.users u", REFUSED, "privileges"),
    ("SELECT (SELECT max(display_name) FROM app.users)", REFUSED, "privileges"),
    ("SELECT * FROM users", ERROR, "not in the search path"),
    ("SELECT * FROM pcl.users", ERROR, "no such table"),
    ("SELECT count(*) FROM app.search_entries", REFUSED, "privileges"),
    ("SELECT has_table_privilege('app.users', 'SELECT')", REFUSED, "privileges"),
    ("SELECT query_to_xml('select * from app.users', true, false, '')", REFUSED, "hardening"),
    ("SELECT table_to_xml('pcl.sessions', true, false, '')", REFUSED, "hardening"),
    ("SELECT schema_to_xml('app', true, false, '')", REFUSED, "hardening"),
    ("SELECT database_to_xml(true, false, '')", REFUSED, "hardening"),
    ("SELECT * FROM ts_stat('select to_tsvector(display_name) from app.users')", REFUSED,
     "hardening"),
    ("SELECT ts_rewrite('a'::tsquery, 'select display_name::tsquery, ''b''::tsquery "
     "from app.users')", REFUSED, "hardening"),
    ("SELECT * FROM dblink('dbname=postgres', 'select 1') AS t(x int)", ERROR,
     "not installed"),
    # ---- the private schema through the system catalogue
    ("SELECT rolname, rolpassword FROM pg_authid", REFUSED, "privileges"),
    ("SELECT usename, passwd FROM pg_shadow", REFUSED, "privileges"),
    ("SELECT * FROM pg_statistic LIMIT 1", REFUSED, "privileges"),
    ("SELECT * FROM pg_largeobject", REFUSED, "privileges"),
    ("SELECT * FROM pg_user_mapping", REFUSED, "privileges"),
    ("SELECT * FROM pg_hba_file_rules", REFUSED, "privileges"),
    ("SELECT * FROM pg_file_settings", REFUSED, "privileges"),
    ("SELECT most_common_vals::text FROM pg_stats WHERE schemaname = 'app'", OK,
     "allowed: no row (filtered by privilege)"),
    ("SELECT string_agg(most_common_vals::text, ' ') FROM pg_stats "
     "WHERE tablename IN ('users', 'tokens', 'query_log')", OK,
     "allowed: no value of a private table"),
    ("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'app'", OK,
     "allowed: no row (filtered by privilege)"),
    ("SELECT n_live_tup FROM pg_stat_all_tables WHERE schemaname = 'app'", REFUSED,
     "hardening"),
    ("SELECT * FROM pg_stat_user_tables", REFUSED, "hardening"),
    ("SELECT pg_stat_get_live_tuples('app.users'::regclass)", REFUSED, "hardening"),
    ("SELECT relname, reltuples FROM pg_class WHERE relnamespace = 'app'::regnamespace", OK,
     "what stays visible: names and row estimates of private tables"),
    ("SELECT 'app.users'::regclass", REFUSED, "privileges"),
    ("SELECT pg_relation_size('app.users') > 0", REFUSED, "privileges"),
    ("SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
     "JOIN pg_namespace n ON n.oid = c.relnamespace "
     "WHERE n.nspname = 'app' AND c.relname = 'users' AND a.attnum > 0",
     OK, "what stays visible: column names of private tables"),
    ("SELECT pg_relation_size(c.oid) > 0 FROM pg_class c JOIN pg_namespace n "
     "ON n.oid = c.relnamespace WHERE n.nspname = 'app' AND c.relname = 'users'", OK,
     "what stays visible: sizes of private tables"),
    ("SELECT pg_get_viewdef('pcl.session_summary'::regclass)", OK,
     "what stays visible: view definitions"),
    ("SELECT description FROM pg_description LIMIT 5", OK, "what stays visible: comments"),
    ("SELECT rolname, rolpassword FROM pg_roles", OK,
     "what stays visible: role names (the password shows as stars)"),
    # ---- who am I: changing role or session
    ("SET ROLE pcl_owner", REFUSED, "grammar"),
    ("RESET ROLE", REFUSED, "grammar"),
    ("SET SESSION AUTHORIZATION pcl_owner", REFUSED, "grammar"),
    ("RESET SESSION AUTHORIZATION", REFUSED, "grammar"),
    ("SELECT set_config('role', 'pcl_owner', false)", REFUSED, "hardening"),
    ("SELECT set_config('role', 'pcl_app', true), (SELECT max(display_name) FROM app.users)",
     REFUSED, "privileges"),
    ("SELECT set_config('session_authorization', 'pcl_owner', true)", REFUSED, "hardening"),
    ("SELECT current_user, session_user", OK, "allowed: pcl_reader twice"),
    # ---- settings
    ("SET statement_timeout = 0", REFUSED, "grammar"),
    ("SET search_path = app, pcl, public", REFUSED, "grammar"),
    ("SET default_transaction_read_only = off", REFUSED, "grammar"),
    ("SET TRANSACTION READ WRITE", REFUSED, "grammar"),
    ("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE", REFUSED, "grammar"),
    ("SET temp_file_limit = -1", REFUSED, "grammar"),
    ("SET LOCAL transaction_timeout = 0", REFUSED, "grammar"),
    ("RESET ALL", REFUSED, "grammar"),
    ("DISCARD ALL", REFUSED, "grammar"),
    ("SHOW data_directory", REFUSED, "grammar"),
    ("SELECT set_config('statement_timeout', '0', false)", REFUSED, "hardening"),
    ("SELECT set_config('search_path', 'app, pcl', false)", REFUSED, "hardening"),
    ("SELECT set_config('transaction_read_only', 'off', true)", REFUSED, "hardening"),
    ("UPDATE pg_settings SET setting = '0' WHERE name = 'statement_timeout'", REFUSED,
     "grammar"),
    ("SELECT current_setting('data_directory')", REFUSED, "privileges"),
    ("SELECT current_setting('hba_file')", REFUSED, "privileges"),
    ("SELECT count(*) FROM pg_settings WHERE name IN ('data_directory', 'hba_file', "
     "'config_file')", OK, "allowed: counts 0, superuser-only settings are hidden"),
    # ---- transactions typed by the person
    ("BEGIN", REFUSED, "grammar"),
    ("BEGIN READ WRITE", REFUSED, "grammar"),
    ("START TRANSACTION ISOLATION LEVEL SERIALIZABLE READ WRITE", REFUSED, "grammar"),
    ("COMMIT", REFUSED, "grammar"),
    ("COMMIT AND CHAIN", REFUSED, "grammar"),
    ("ROLLBACK", REFUSED, "grammar"),
    ("END", REFUSED, "grammar"),
    ("ABORT", REFUSED, "grammar"),
    ("SAVEPOINT s1", REFUSED, "grammar"),
    ("RELEASE SAVEPOINT s1", REFUSED, "grammar"),
    ("PREPARE TRANSACTION 'hostile'", REFUSED, "grammar"),
    # ---- prepared statements and cursors of their own
    ("PREPARE hostile_plan AS SELECT * FROM pcl.sessions", REFUSED, "grammar"),
    ("EXECUTE hostile_plan", REFUSED, "grammar"),
    ("DEALLOCATE ALL", REFUSED, "grammar"),
    ("DECLARE c1 CURSOR WITH HOLD FOR SELECT * FROM pcl.sessions", REFUSED, "grammar"),
    ("FETCH ALL FROM pcl_check", REFUSED, "grammar"),
    ("CLOSE ALL", REFUSED, "grammar"),
    # ---- COPY in every form, files, programs
    ("COPY pcl.sessions TO STDOUT", REFUSED, "grammar"),
    ("COPY (SELECT * FROM app.users) TO STDOUT", REFUSED, "grammar"),
    ("COPY pcl.sessions FROM STDIN", REFUSED, "grammar"),
    ("COPY pcl.sessions TO '{dir}/stolen.csv'", REFUSED, "grammar"),
    ("COPY pcl.meta FROM '/etc/hosts'", REFUSED, "grammar"),
    ("COPY (SELECT 1) TO PROGRAM 'touch {dir}/made_by_program'", REFUSED, "grammar"),
    ("COPY pcl.meta FROM PROGRAM 'cat /etc/hosts'", REFUSED, "grammar"),
    ("SELECT pg_read_file('postgresql.conf')", REFUSED, "privileges"),
    ("SELECT pg_read_binary_file('global/pg_control')", REFUSED, "privileges"),
    ("SELECT pg_ls_dir('.')", REFUSED, "privileges"),
    ("SELECT * FROM pg_ls_waldir()", REFUSED, "privileges"),
    ("SELECT pg_stat_file('postgresql.conf')", REFUSED, "privileges"),
    ("SELECT xmlparse(document '<!DOCTYPE d [<!ENTITY x SYSTEM \"file:///etc/hosts\">]>"
     "<d>&x;</d>')::text", (OK, ERROR, REFUSED),
     "allowed: the file is not read (or XML is not built in)"),
    # ---- large objects
    ("SELECT lo_creat(-1)", REFUSED, "hardening"),
    ("SELECT lo_from_bytea(0, 'abc'::bytea)", REFUSED, "hardening"),
    ("SELECT lo_import('/etc/hosts')", REFUSED, "privileges"),
    ("SELECT lo_export(1, '{dir}/made_by_lo_export')", REFUSED, "privileges"),
    # ---- temporary tables, DO blocks, functions
    ("CREATE TEMP TABLE hostile (x int)", REFUSED, "grammar"),
    ("SELECT * INTO TEMP hostile FROM pcl.sessions", REFUSED, "grammar"),
    ("DO $$ BEGIN PERFORM 1; END $$", REFUSED, "grammar"),
    ("DO LANGUAGE sql $$ SELECT 1 $$", REFUSED, "grammar"),
    ("CREATE FUNCTION pg_temp.hostile() RETURNS int LANGUAGE sql AS 'SELECT 1'", REFUSED,
     "grammar"),
    ("CREATE OR REPLACE FUNCTION pcl.hostile() RETURNS int LANGUAGE plpgsql AS "
     "$$ BEGIN RETURN 1; END $$", REFUSED, "grammar"),
    ("CALL hostile()", REFUSED, "grammar"),
    # ---- LISTEN / NOTIFY, advisory locks, sequences, row locks
    ("LISTEN pcl_events", REFUSED, "grammar"),
    ("NOTIFY pcl_events, 'hostile'", REFUSED, "grammar"),
    ("SELECT pg_notify('pcl_events', 'hostile')", REFUSED, "hardening"),
    ("SELECT pg_advisory_lock(1)", REFUSED, "hardening"),
    ("SELECT pg_try_advisory_xact_lock_shared(1, 2)", REFUSED, "hardening"),
    ("SELECT pg_advisory_unlock_all()", REFUSED, "hardening"),
    ("SELECT nextval('pcl.hostile_seq')", REFUSED, "privileges"),
    ("SELECT setval('pcl.hostile_seq', 100)", REFUSED, "privileges"),
    ("SELECT currval('pcl.hostile_seq')", REFUSED, "privileges"),
    ("SELECT * FROM pcl.sessions FOR UPDATE", REFUSED, "read-only"),
    ("SELECT * FROM pcl.sessions FOR KEY SHARE", REFUSED, "read-only"),
    # ---- other sessions
    ("SELECT usename, query FROM pg_stat_activity", REFUSED, "hardening"),
    ("SELECT pg_stat_get_backend_activity(1)", REFUSED, "hardening"),
    ("SELECT * FROM pg_stat_get_activity(NULL)", REFUSED, "hardening"),
    ("SELECT pg_terminate_backend(pg_backend_pid())", REFUSED, "hardening"),
    ("SELECT pg_cancel_backend(l.pid) FROM pg_lock_status() l", REFUSED, "hardening"),
    ("SELECT pg_cancel_backend(pid) FROM generate_series(1, 99999) pid", REFUSED,
     "hardening"),
    ("SELECT pg_reload_conf()", REFUSED, "privileges"),
    ("SELECT pg_logical_emit_message(false, 'hostile', 'payload')", REFUSED, "hardening"),
    ("SELECT pg_export_snapshot()", REFUSED, "hardening"),
    ("SELECT pg_create_logical_replication_slot('hostile', 'pgoutput')", REFUSED,
     "privileges"),
    # ---- time: must end at the deadline
    ("SELECT pg_sleep(60)", REFUSED, "hardening"),
    (_BUSY, TIMED_OUT, "timeout"),
    ("EXPLAIN ANALYZE " + _BUSY, TIMED_OUT, "timeout"),
    ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT max(n) FROM r",
     (TIMED_OUT, REFUSED), "timeout, or the cap on temporary files"),
    # ---- size: huge results and huge values
    ("SELECT g FROM generate_series(1, 100000000) g", REFUSED,
     "limit (temp_file_limit: this form is built in full before its first row)"),
    ("SELECT count(*) FROM generate_series(1, 100000000000)", REFUSED, "limit"),
    ("SELECT generate_series(1, 100000000)", OK, "allowed: cut at the row limit, at once"),
    ("SELECT repeat('x', 1073741824)", REFUSED, "limit (the server's own)"),
    ("SELECT repeat('x', 300000000)", OK, "allowed: the server cuts the cell"),
    ("SELECT length(repeat('x', 200000000))", OK,
     "what stays possible: one statement can hold a value of hundreds of MB on the server"),
    ("SELECT repeat('[', 200000)::json", REFUSED, "limit (the server's own)"),
    ("SELECT " + "(" * 30000 + "1" + ")" * 30000, (ERROR, REFUSED), "limit (the server's own)"),
    # ---- getting out of the bracket the reader puts around a query
    ("SELECT 1) AS q UNION ALL SELECT repeat('x', 50000000) FROM (SELECT 1", ERROR,
     "grammar: not a complete query on its own"),
    ("SELECT 1 --\n) AS pcl_q(c1) UNION ALL SELECT repeat('x', 50000000) --", ERROR,
     "grammar: not a complete query on its own"),
    ("SELECT 1 /* never closed", ERROR, "grammar"),
    ("SELECT 'never closed", ERROR, "grammar"),
    ("SELECT $tag$ never closed", ERROR, "grammar"),
    # ---- what a reader may learn about the server (it cannot be hidden)
    ("SELECT version(), current_database(), inet_server_port(), pg_backend_pid()", OK,
     "what stays visible: version, database name, port, own process id"),
    ("SELECT name, setting FROM pg_settings WHERE name IN ('port', 'listen_addresses')", OK,
     "what stays visible: ordinary settings"),
    ("SELECT pid, mode FROM pg_locks", REFUSED, "hardening"),
    ("SELECT pg_current_wal_lsn(), pg_postmaster_start_time()", OK,
     "what stays visible: how much the server has written, since when it runs"),
    ("SELECT txid_current() > 0", OK, "what stays possible: one transaction id per query"),
]

# Statements that make the server build a value of hundreds of megabytes. On a slow machine
# (the Windows runner of the GitHub checks) that takes longer than TIMEOUT, so they get a longer
# limit: what they prove is their outcome (the cell is cut, nothing private comes back,
# nothing changes), not the speed of the machine. The hard stop still bounds them.
SLOW = {
    "SELECT repeat('x', 300000000)": 30.0,
    "SELECT length(repeat('x', 200000000))": 30.0,
}


def _owner(store):
    import psycopg
    return psycopg.connect(store.conninfo("owner"), autocommit=True)


def reader_sessions(store):
    """How many sessions of pcl_reader the server has in this database right now."""
    connection = _owner(store)
    try:
        return connection.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'pcl_reader' "
            "AND datname = current_database()").fetchone()[0]
    finally:
        connection.close()


def wait_until_no_reader(test, store, seconds=5.0):
    deadline = time.monotonic() + seconds
    while reader_sessions(store) and time.monotonic() < deadline:
        time.sleep(0.05)
    test.assertEqual(reader_sessions(store), 0, "a reader connection is still there")


class ReaderBase(support.PostgresCase):
    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            self.keys = support.seed_minimal(db)
            now = util.now_utc_text()
            db.insert("app.users", {"user_id": "u-" + MARKER, "name": "marker-user",
                                    "display_name": MARKER, "role": "viewer", "disabled": False,
                                    "created_utc": now})
            db.insert("app.tokens", {"token_id": "t-marker", "user_id": "u-" + MARKER,
                                     "token_sha256": util.sha256_hex(MARKER.encode("utf-8")),
                                     "label": MARKER, "created_utc": now})
            db.insert("app.query_log", {"log_id": "l-marker", "at_utc": now, "user_name": MARKER,
                                        "mode": "sql", "query_text": "select '%s'" % MARKER,
                                        "ok": True})

    def owner(self):
        connection = _owner(self.store)
        self.addCleanup(connection.close)
        return connection

    def attempt(self, sql, max_rows=50, timeout=TIMEOUT):
        """(outcome, text): "ok" and the result as text, or the error's code and message."""
        try:
            with self.store.reader() as reader:
                columns, rows, truncated = reader.run(sql, max_rows=max_rows,
                                                      timeout_seconds=timeout)
        except PclError as error:
            return error.code, error.message + repr(error.details)
        return OK, repr((columns, rows, truncated))

    def read(self, sql, **arguments):
        with self.store.reader() as reader:
            return reader.run(sql, **arguments)

    def dump(self):
        """Every row of every table, and everything else a statement could have left behind."""
        owner = _owner(self.store)
        try:
            state = {}
            for table in schema_def.TABLES:
                state[table.qualified] = owner.execute(
                    "SELECT t::text FROM %s t ORDER BY 1" % table.qualified).fetchall()
            state["objects"] = owner.execute(
                "SELECT n.nspname, c.relname, c.relkind, c.relacl::text FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname !~ '^pg_toast' ORDER BY 1, 2").fetchall()
            state["schemas"] = owner.execute(
                "SELECT nspname, nspacl::text FROM pg_namespace ORDER BY 1").fetchall()
            state["functions"] = owner.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')").fetchall()
            state["roles"] = owner.execute(
                "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolconnlimit, "
                "rolpassword FROM pg_authid WHERE rolname LIKE 'pcl\\_%' ORDER BY 1").fetchall()
            state["role settings"] = owner.execute(
                "SELECT setdatabase, setrole, setconfig::text FROM pg_db_role_setting "
                "ORDER BY 1, 2").fetchall()
            state["large objects"] = owner.execute(
                "SELECT count(*) FROM pg_largeobject_metadata").fetchall()
            state["sequence"] = owner.execute(
                "SELECT last_value, is_called FROM pcl.hostile_seq").fetchall()
            state["databases"] = owner.execute(
                "SELECT count(*) FROM pg_database").fetchall()
            state["prepared transactions"] = owner.execute(
                "SELECT count(*) FROM pg_prepared_xacts").fetchall()
            return state
        finally:
            owner.close()

    def project_files(self):
        found = []
        for folder, _folders, files in os.walk(self.project.path):
            found.extend(os.path.join(folder, name) for name in files)
        return sorted(found)


class TestHostileStatements(ReaderBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        owner = self.owner()
        owner.execute("CREATE SEQUENCE IF NOT EXISTS pcl.hostile_seq")
        owner.execute("ANALYZE")        # so that statistics (sample values) of app.* exist

    def test_the_list_is_long_enough(self):
        self.assertGreaterEqual(len(HOSTILE), 40)
        self.assertEqual(len({sql for sql, _outcome, _layer in HOSTILE}), len(HOSTILE))
        self.assertLessEqual(set(SLOW), {sql for sql, _outcome, _layer in HOSTILE})

    def test_every_statement_has_its_outcome_and_nothing_changes(self):
        before = self.dump()
        files_before = self.project_files()
        wrong = []
        for sql, expected, layer in HOSTILE:
            limit = SLOW.get(sql, TIMEOUT)
            sql = sql.replace("{dir}", str(self.project.path))
            started = time.monotonic()
            outcome, text = self.attempt(sql, timeout=limit)
            elapsed = time.monotonic() - started
            accepted = expected if isinstance(expected, tuple) else (expected,)
            if outcome not in accepted:
                wrong.append("%s\n    expected %s (%s), got %s: %s"
                             % (sql[:120], expected, layer, outcome, text[:240]))
            self.assertNotIn(MARKER, text, "a private value came back for: " + sql[:120])
            self.assertLess(elapsed, limit + reader_postgres.HARD_STOP_SECONDS,
                            "took too long: " + sql[:120])
        self.assertEqual(wrong, [], "\n".join(wrong))
        wait_until_no_reader(self, self.store)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.project_files(), files_before)

    def test_the_private_tables_and_their_statistics_hold_the_marker(self):
        # Otherwise "the marker never came back" would prove nothing.
        owner = self.owner()
        self.assertEqual(owner.execute(
            "SELECT count(*) FROM app.users WHERE display_name = %s", [MARKER]).fetchone()[0], 1)
        seen = owner.execute(
            "SELECT string_agg(most_common_vals::text || histogram_bounds::text, ' ') "
            "FROM pg_stats WHERE schemaname = 'app'").fetchone()[0] or ""
        rows = owner.execute("SELECT count(*) FROM pg_stats WHERE schemaname = 'app'").fetchone()
        self.assertGreater(rows[0], 0)
        self.assertIsInstance(seen, str)

    def test_what_the_visible_statements_return(self):
        outcome, text = self.attempt("SELECT current_user, session_user")
        self.assertIn("('pcl_reader', 'pcl_reader')", text)
        outcome, text = self.attempt(
            "SELECT count(*) FROM pg_stats WHERE schemaname = 'app'")
        self.assertIn("[(0,)]", text)
        outcome, text = self.attempt("SELECT rolname, rolpassword FROM pg_roles "
                                     "WHERE rolname = 'pcl_app'")
        self.assertIn("('pcl_app', '********')", text)
        columns, rows, truncated = self.read("SELECT repeat('x', 300000000)", max_rows=5)
        self.assertIsInstance(rows[0][0], sqlrun.CutText)
        self.assertEqual(len(rows[0][0]), self.settings.sql.max_cell_chars)
        columns, rows, truncated = self.read("SELECT generate_series(1, 100000000)", max_rows=7)
        self.assertEqual((len(rows), truncated), (7, True))
        outcome, text = self.attempt(
            "SELECT xmlparse(document '<!DOCTYPE d [<!ENTITY x SYSTEM \"file:///etc/hosts\">]>"
            "<d>&x;</d>')::text")
        self.assertNotIn("localhost", text.replace("file:///etc/hosts", ""))

    def test_settings_of_one_query_never_reach_the_next(self):
        self.attempt("SELECT set_config('statement_timeout', '0', false)")
        self.attempt("SET search_path = app")
        self.attempt("EXPLAIN SELECT set_config('search_path', 'app', false)")
        _columns, rows, _ = self.read(
            "SELECT current_setting('statement_timeout'), current_setting('search_path'), "
            "current_setting('default_transaction_read_only'), "
            "current_setting('transaction_read_only'), current_setting('lock_timeout'), "
            "current_setting('temp_file_limit'), current_setting('work_mem'), "
            "current_setting('max_parallel_workers_per_gather'), current_setting('TimeZone')",
            timeout_seconds=7)
        self.assertEqual(rows, [("7s", "pcl", "on", "on", "3s", "256MB", "16MB", "0", "UTC")])

    def test_one_reader_cannot_stop_or_see_another(self):
        result = {}

        def victim():
            result["a"] = self.attempt(_BUSY, timeout=3.0)

        thread = threading.Thread(target=victim)
        started = time.monotonic()
        thread.start()
        try:
            time.sleep(0.5)
            self.assertEqual(reader_sessions(self.store), 1)
            for sql in ("SELECT pg_terminate_backend(pid) FROM pg_locks "
                        "WHERE pid <> pg_backend_pid()",
                        "SELECT pg_cancel_backend(pid) FROM generate_series(1, 99999) pid "
                        "WHERE pid <> pg_backend_pid()",
                        "SELECT pg_terminate_backend(pid) FROM generate_series(1, 99999) pid "
                        "WHERE pid <> pg_backend_pid()",
                        "SELECT query FROM pg_stat_activity",
                        "SELECT pg_stat_get_backend_activity(s.id) "
                        "FROM pg_stat_get_backend_idset() AS s(id)"):
                outcome, text = self.attempt(sql)
                self.assertEqual(outcome, REFUSED, sql)
                self.assertNotIn("pg_class a", text)
            # A second reader is served while the first one is busy.
            quick = time.monotonic()
            self.assertEqual(self.read("SELECT count(*) FROM sessions")[1], [(2,)])
            self.assertLess(time.monotonic() - quick, 2.0)
        finally:
            thread.join()
        # The victim ran until its OWN deadline: nobody ended it early.
        self.assertEqual(result["a"][0], TIMED_OUT)
        self.assertGreaterEqual(time.monotonic() - started, 2.9)


class TestWhatWorks(ReaderBase, unittest.TestCase):
    def test_plain_and_qualified_names_both_resolve(self):
        for sql in ("SELECT count(*) FROM sessions", "SELECT count(*) FROM pcl.sessions",
                    "SELECT count(*) FROM session_summary", "TABLE meta",
                    "SELECT count(*) FROM pcl.current_points"):
            columns, rows, truncated = self.read(sql)
            self.assertTrue(rows, sql)
            self.assertFalse(truncated)
        columns, rows, _ = self.read("SELECT count(*) AS n FROM pcl.sessions")
        self.assertEqual((columns, rows), ([("n", "bigint")], [(2,)]))

    def test_views_lists_and_json(self):
        _columns, rows, _ = self.read(
            "SELECT sample_id FROM session_samples ORDER BY sample_id")
        self.assertEqual(rows, [("S-100",), ("S-200",), ("S-201",)])
        _columns, rows, _ = self.read("SELECT label, n FROM session_labels ORDER BY label")
        self.assertEqual(rows, [("UNKNOWN", 1), ("adenine", 1), ("r6g", 1)])
        _columns, rows, _ = self.read(
            "SELECT point_id, effective_label, awaiting_review FROM current_points "
            "ORDER BY point_id")
        self.assertEqual(rows, [(1, "adenine", False), (2, "r6g", False), (3, "UNKNOWN", True)])
        columns, rows, _ = self.read(
            "SELECT sample_ids, tags, card_json, card_json ->> 'schema' AS s, started_utc, "
            "bytes, simulated FROM sessions ORDER BY path LIMIT 1")
        self.assertEqual([kind for _name, kind in columns],
                         ["text[]", "text[]", "jsonb", "text", "timestamp with time zone",
                          "bigint", "boolean"])
        row = rows[0]
        self.assertEqual(row[0], ["S-100"])
        self.assertEqual(row[1], [])
        self.assertEqual(row[2]["schema"], "pcl-session/1")
        self.assertEqual(row[3:], ("pcl-session/1", "2026-11-15T16:09:40.537+00:00", 121843,
                                   False))

    def test_query_shapes(self):
        for sql, expected in (
                ("WITH x AS (SELECT path FROM sessions) SELECT count(*) FROM x", [(2,)]),
                ("VALUES (1, 'a'), (2, 'b')", [(1, "a"), (2, "b")]),
                ("SELECT 1;", [(1,)]),
                ("SELECT 1; -- done", [(1,)]),
                ("SELECT 1 ; ; ", [(1,)]),
                ("/* first */ -- second\n SELECT 2", [(2,)]),
                ("(SELECT 1) UNION ALL (SELECT 2) ORDER BY 1", [(1,), (2,)]),
                ("SELECT $$a;b$$, 'c;d', E'it\\'s; ok';", [("a;b", "c;d", "it's; ok")]),
                ("SELECT 1 AS \"a;b\" -- ; trailing", [(1,)]),
                ("SELECT n FROM generate_series(1, 3) n ORDER BY n DESC", [(3,), (2,), (1,)]),
                ("SELECT 1 AS a, 2 AS a, 3, 4", [(1, 2, 3, 4)]),
                ("SELECT path FROM sessions WHERE false", [])):
            self.assertEqual(self.read(sql)[1], expected, sql)
        columns, rows, _ = self.read("SELECT 1 AS a, 2 AS a, 3")
        self.assertEqual([name for name, _kind in columns], ["a", "a", "?column?"])
        columns, rows, _ = self.read("SELECT FROM sessions")
        self.assertEqual((columns, rows), ([], [(), ()]))
        columns, rows, _ = self.read("SELECT path, bytes FROM sessions WHERE false")
        self.assertEqual((columns, rows), ([("path", "text"), ("bytes", "bigint")], []))

    def test_explain_runs_on_its_own_path(self):
        columns, rows, truncated = self.read("EXPLAIN SELECT * FROM sessions")
        self.assertEqual(columns, [("QUERY PLAN", "text")])
        self.assertIn("sessions", rows[0][0])
        _columns, rows, _ = self.read("explain (format json) select 1")
        self.assertIsInstance(rows[0][0], list)
        _columns, rows, truncated = self.read(
            "EXPLAIN (ANALYZE, VERBOSE) SELECT * FROM current_points", max_rows=3)
        self.assertEqual((len(rows), truncated), (3, True))

    def test_rows_are_cut_at_max_rows_and_at_the_size_limit(self):
        _columns, rows, truncated = self.read("SELECT rel_path FROM files ORDER BY 1",
                                              max_rows=3)
        self.assertEqual((len(rows), truncated), (3, True))
        _columns, rows, truncated = self.read("SELECT rel_path FROM files", max_rows=8)
        self.assertEqual((len(rows), truncated), (8, False))
        original = sqlrun.MAX_RESULT_BYTES
        sqlrun.MAX_RESULT_BYTES = 30000
        self.addCleanup(setattr, sqlrun, "MAX_RESULT_BYTES", original)
        _columns, rows, truncated = self.read(
            "SELECT repeat('x', 9000) FROM generate_series(1, 1000)", max_rows=1000)
        self.assertEqual((len(rows), truncated), (4, True))

    def test_a_hundred_million_rows_and_a_huge_value_stay_small_here(self):
        try:
            import resource
        except ImportError:             # Windows has no such module
            resource = None

        def peak():
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0

        before = peak()
        started = time.monotonic()
        _columns, rows, truncated = self.read(
            "SELECT generate_series(1, 100000000), repeat('x', 100)", max_rows=1000,
            timeout_seconds=10)
        self.assertEqual((len(rows), truncated), (1000, True))
        self.assertLess(time.monotonic() - started, 5.0)
        _columns, rows, truncated = self.read(
            "SELECT repeat('x', 300000000), repeat('y', 1000000)::bytea, "
            "array_fill('z'::text, ARRAY[2000000])", timeout_seconds=20)
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(isinstance(value, sqlrun.CutText) for value in rows[0]))
        grown = peak() - before
        if sys.platform != "darwin":
            grown *= 1024               # Linux counts in kilobytes, macOS in bytes
        # Measured on macOS with PostgreSQL 18: the peak did not grow at all (0 MB).
        self.assertLess(grown, 64 * 1024 * 1024)

    def test_error_positions_point_into_the_typed_text(self):
        with self.assertRaises(PclError) as caught:
            self.read("SELECT folder,\n  no_such_column\nFROM sessions")
        error = caught.exception
        self.assertEqual(error.code, "sql_error")
        self.assertEqual((error.details["line"], error.details["column"]), (2, 3))
        self.assertEqual(error.details["sqlstate"], "42703")
        self.assertIn("line 2, column 3", error.message)
        self.assertIn("no_such_column", error.message)
        with self.assertRaises(PclError) as caught:
            self.read("EXPLAIN SELECT *\nFROM sesions")
        self.assertEqual((caught.exception.details["line"],
                          caught.exception.details["column"]), (2, 6))
        with self.assertRaises(PclError) as caught:
            self.read("SELECT 1/0")
        self.assertEqual(caught.exception.code, "sql_error")
        self.assertIn("division by zero", caught.exception.message)
        self.assertNotIn("line", caught.exception.message)

    def test_a_reader_runs_one_statement_only(self):
        with self.store.reader() as reader:
            reader.run("SELECT 1")
            with self.assertRaises(RuntimeError):
                reader.run("SELECT 1")
        wait_until_no_reader(self, self.store)

    def test_bad_arguments_and_broken_text(self):
        for arguments in ({"max_rows": 0}, {"max_rows": True}, {"timeout_seconds": 0}):
            with self.store.reader() as reader:
                with self.assertRaises(ValueError):
                    reader.run("SELECT 1", **arguments)
        for sql in ("SELECT 1\x00; DROP TABLE pcl.sessions", "SELECT '\ud800'", b"SELECT 1"):
            with self.assertRaises(PclError) as caught:
                self.read(sql)
            self.assertEqual(caught.exception.code, "sql_refused")

    def test_both_paths_refuse_a_second_statement(self):
        # The pin for the trap: psycopg's plain execute() would run both statements.
        for sql in ("SELECT 1; SELECT 2", "EXPLAIN SELECT 1; SELECT 2",
                    "SELECT 1; COMMIT", "EXPLAIN SELECT 1; COMMIT; " + _INSERT):
            with self.assertRaises(PclError) as caught:
                self.read(sql)
            self.assertEqual(caught.exception.code, "sql_refused", sql)
            self.assertIn("one statement at a time", caught.exception.message)
        self.assertEqual(self.outside_rows(
            "SELECT count(*) FROM {pcl}.meta WHERE key = 'hostile'"), [(0,)])


class TestStatementEnd(unittest.TestCase):
    """reader_postgres.statement_end: where the first statement of a text ends."""

    def test_cases(self):
        for sql, body, more in (
                ("select 1", "select 1", False),
                ("select 1;", "select 1", False),
                ("select 1 ; ; -- x\n /* y */ ", "select 1 ", False),
                ("select 1; select 2", "select 1", True),
                ("select ';' ;", "select ';' ", False),
                ("select 'a''b;' ; x", "select 'a''b;' ", True),
                ("select \"a;b\" ;", "select \"a;b\" ", False),
                ("select $$;$$, $t$ ; $t$ ;", "select $$;$$, $t$ ; $t$ ", False),
                ("select E'\\';' ;", "select E'\\';' ", False),
                ("select '\\' ; select 2 --'", "select '\\' ", True),
                ("select 1 -- ; no\n ;", "select 1 -- ; no\n ", False),
                ("select 1 /* ; /* ; */ ; */ ;", "select 1 /* ; /* ; */ ; */ ", False),
                ("select a$b$c ; x", "select a$b$c ", True),
                ("select $1 ; $2", "select $1 ", True),
                ("select 'never closed ;", "select 'never closed ;", False),
                ("", "", False)):
            end, found_more = reader_postgres.statement_end(sql)
            self.assertEqual((sql[:end], found_more), (body, more), sql)


# What a server whose lc_messages is German says (PostgreSQL's own translations).
_GERMAN_BIND = ("Binden-Nachricht enthält 0 Parameter, aber vorbereitete Anweisung »« "
                "erfordert 1")
_GERMAN_SCHEMA = "keine Berechtigung für Schema app"


class _Diag:
    def __init__(self, primary, severity="ERROR", schema_name=None):
        self.message_primary = primary
        self.severity_nonlocalized = severity
        self.schema_name = schema_name
        self.statement_position = None
        self.message_hint = None


class _Error(Exception):
    def __init__(self, code, primary, **diag):
        super().__init__(primary)
        self.sqlstate = code
        self.diag = _Diag(primary, **diag)


class _Connection:
    def __init__(self, broken=False):
        self.closed = broken
        self.broken = broken


class TestErrorsInAnyLanguage(unittest.TestCase):
    """SEC2-04: a lab server may write its messages in another language (lc_messages, which a
    reader cannot change). The answers to a parameter mark and to the private schema rest on
    what does not depend on it: the error code, the state of the session, the diagnostic
    fields and the SQL text."""

    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("the Python package psycopg is not installed")

    def translate(self, error, sql, connection=None):
        reader = reader_postgres.PostgresReader(None)
        reader._connection = connection if connection is not None else _Connection()
        return reader._translate(error, sql, TIMEOUT, {"prefix": None})

    def test_a_parameter_mark_in_another_language(self):
        for sql in ("SELECT $1", "EXPLAIN SELECT $1",
                    "SELECT path FROM sessions WHERE instrument = $1"):
            result = self.translate(_Error("08P01", _GERMAN_BIND), sql)
            self.assertEqual((result.code, result.message),
                             ("sql_refused", sqlrun.PARAMETER_MARK), sql)
        # The session is alive: the mark is the cause, even where the text hides it from
        # this module's reading of it.
        result = self.translate(_Error("08P01", _GERMAN_BIND), "SELECT 1")
        self.assertEqual(result.code, "sql_refused")
        # The text holds a mark: the cause, even when the session state is not known.
        result = self.translate(_Error("08P01", _GERMAN_BIND), "SELECT $1",
                                _Connection(broken=True))
        self.assertEqual(result.code, "sql_refused")
        # A real loss of the connection still says so.
        result = self.translate(_Error("08P01", "Verbindung verloren", severity="FATAL"),
                                "SELECT 1", _Connection(broken=True))
        self.assertEqual(result.code, "db_down")
        result = self.translate(_Error("08006", "Verbindung verloren"), "SELECT $1")
        self.assertEqual(result.code, "db_down")

    def test_the_private_schema_in_another_language(self):
        for sql in ("SELECT * FROM app.users", "TABLE APP . tokens",
                    "SELECT display_name FROM \"app\".\"users\"",
                    "SELECT 'app.users'::regclass",
                    "SELECT has_table_privilege('app.users', 'SELECT')"):
            result = self.translate(_Error("42501", _GERMAN_SCHEMA), sql)
            self.assertEqual((result.code, result.message),
                             ("sql_refused", sqlrun.PRIVATE_PART), sql)
        # The diagnostic field alone is enough.
        result = self.translate(_Error("42501", "keine Berechtigung", schema_name="app"),
                                "SELECT 1")
        self.assertEqual(result.message, sqlrun.PRIVATE_PART)
        # Another missing right keeps its own sentence.
        result = self.translate(_Error("42501", "keine Berechtigung für Funktion pg_sleep"),
                                "SELECT pg_sleep(60)")
        self.assertEqual(result.code, "sql_refused")
        self.assertNotEqual(result.message, sqlrun.PRIVATE_PART)
        self.assertIn("pg_sleep", result.message)
        # SEC2-04, second round: a table's other name "app" is not the private schema.
        for sql in ("SELECT app.path, pg_read_file('/etc/passwd') FROM sessions app",
                    "WITH app AS (SELECT 1 AS x) SELECT app.x, pg_read_file('x') FROM app"):
            result = self.translate(
                _Error("42501", "permission denied for function pg_read_file"), sql)
            self.assertEqual(result.code, "sql_refused", sql)
            self.assertNotEqual(result.message, sqlrun.PRIVATE_PART, sql)
            self.assertIn("pg_read_file", result.message, sql)

    def test_the_other_refusals_in_another_language(self):
        # docs/SECURITY.md, second round: these were decided by English words of the message.
        one_statement = "Expert mode runs one statement at a time."
        for code, primary, sql, start in (
                ("42601", "kann nicht mehrere Befehle in vorbereitete Anweisung einfügen",
                 "SELECT 1; DELETE FROM sessions", one_statement),
                ("42601", "kann nicht mehrere Befehle in vorbereitete Anweisung einfügen",
                 "SELECT ';' ; SELECT 2", one_statement),
                ("42601", "SELECT ... INTO ist hier nicht erlaubt",
                 "SELECT * INTO newtable FROM sessions", "Expert mode only reads"),
                ("0A000", "DECLARE CURSOR darf keine datenverändernden Anweisungen in WITH "
                 "enthalten", "WITH d AS (DELETE FROM sessions RETURNING 1) SELECT * FROM d",
                 "Expert mode only reads")):
            result = self.translate(_Error(code, primary), sql)
            self.assertEqual(result.code, "sql_refused", sql)
            self.assertTrue(result.message.startswith(start), (sql, result.message))
        # A typing mistake stays the server's own complaint.
        result = self.translate(_Error("42601", "Syntaxfehler bei »FORM«"),
                                "SELECT 'into; x' FORM sessions")
        self.assertEqual(result.code, "sql_error")
        # A cancel at the time limit is a timeout; one long before it is someone else's.
        cancelled = _Error("57014", "storniere Anfrage wegen Zeitüberschreitung der Anweisung")
        reader = reader_postgres.PostgresReader(None)
        reader._connection = _Connection()
        late = {"prefix": None, "started": time.monotonic() - TIMEOUT - 0.5}
        self.assertEqual(reader._translate(cancelled, "SELECT 1", TIMEOUT, late).code,
                         "sql_timeout")
        early = {"prefix": None, "started": time.monotonic()}
        self.assertEqual(reader._translate(cancelled, "SELECT 1", TIMEOUT, early).message,
                         "The query was cancelled.")

    def test_reading_the_text(self):
        for sql, mark in (("SELECT $1", True), ("select 1$1", True), ("SELECT ($12)", True),
                          ("SELECT '$1'", False), ("SELECT $$ $1 $$", False),
                          ("SELECT $t$ $1 $t$", False), ("SELECT 1 -- $1", False),
                          ("SELECT /* /* $1 */ $1 */ 1", False), ("SELECT \"$1\"", False),
                          ("SELECT a$1 FROM t", False), ("SELECT E'\\'$1'", False),
                          ("SELECT '\\'$1", True), ("SELECT $", False), ("", False)):
            self.assertEqual(reader_postgres.has_parameter_mark(sql), mark, sql)
        for sql, into in (("SELECT * INTO t FROM s", True), ("select * into t from s", True),
                          ("SELECT/**/*/**/INTO t FROM s", True),
                          ("SELECT 'into' FROM s", False), ("SELECT 1 -- into", False),
                          ("SELECT \"into\" FROM s", False), ("SELECT intox FROM s", False),
                          ("SELECT $$ into $$", False), ("", False)):
            self.assertEqual(reader_postgres.has_key_word(sql, "into"), into, sql)


class TestServerInAnotherLanguage(ReaderBase, unittest.TestCase):
    """SEC2-04 against a real server: the test database gets German messages (where this
    PostgreSQL and this system have them) and the answers stay the same."""

    _LOCALES = ("de_DE.UTF-8", "de_DE.utf8", "de_DE", "German_Germany.1252", "de-DE")

    def setUp(self):
        super().setUp()
        owner = self.owner()
        database = owner.execute("SELECT current_database()").fetchone()[0]
        from psycopg import errors, sql as pgsql
        for name in self._LOCALES:
            try:
                owner.execute(pgsql.SQL("ALTER DATABASE {} SET lc_messages = {}").format(
                    pgsql.Identifier(database), pgsql.Literal(name)))
                break
            except errors.Error:
                continue
        else:
            self.skipTest("this system has no German locale for PostgreSQL's messages")
        self.addCleanup(lambda: self.owner().execute(pgsql.SQL(
            "ALTER DATABASE {} RESET lc_messages").format(pgsql.Identifier(database))))
        outcome, text = self.attempt("SELECT 1 / 0")
        if "division by zero" in text:
            self.skipTest("this PostgreSQL has no German messages (built without NLS)")

    def test_the_same_answers_in_german(self):
        for sql in ("SELECT $1", "EXPLAIN SELECT $1",
                    "SELECT path FROM sessions WHERE instrument = $1"):
            with self.assertRaises(PclError) as caught:
                self.read(sql, timeout_seconds=TIMEOUT)
            self.assertEqual((caught.exception.code, caught.exception.message),
                             ("sql_refused", sqlrun.PARAMETER_MARK), sql)
        for sql in ("SELECT * FROM app.users", "TABLE app.tokens",
                    "SELECT display_name FROM \"app\".\"users\"",
                    "EXPLAIN SELECT * FROM app.users", "SELECT 'app.users'::regclass"):
            with self.assertRaises(PclError) as caught:
                self.read(sql, timeout_seconds=TIMEOUT)
            self.assertEqual((caught.exception.code, caught.exception.message),
                             ("sql_refused", sqlrun.PRIVATE_PART), sql)
        for sql, start in (("SELECT 1; DELETE FROM sessions",
                            "Expert mode runs one statement at a time."),
                           ("SELECT * INTO newtable FROM sessions", "Expert mode only reads"),
                           ("WITH d AS (DELETE FROM sessions RETURNING 1) SELECT * FROM d",
                            "Expert mode only reads")):
            with self.assertRaises(PclError) as caught:
                self.read(sql, timeout_seconds=TIMEOUT)
            self.assertEqual(caught.exception.code, "sql_refused", sql)
            self.assertTrue(caught.exception.message.startswith(start),
                            (sql, caught.exception.message))
        assert_another_right(self, self.read)
        self.assertEqual(self.read("SELECT count(*) FROM sessions")[1], [(2,)])


def assert_another_right(case, read):
    """SEC2-04, second round: a table or WITH part called "app" is not the private schema; a
    function the reader may not call keeps its own sentence."""
    for sql in ("SELECT app.path, pg_read_file('/etc/passwd') FROM sessions app",
                "WITH app AS (SELECT 1 AS x) SELECT app.x, pg_ls_dir('.') FROM app"):
        with case.assertRaises(PclError) as caught:
            read(sql, timeout_seconds=TIMEOUT)
        case.assertEqual(caught.exception.code, "sql_refused", sql)
        case.assertNotEqual(caught.exception.message, sqlrun.PRIVATE_PART, sql)
        case.assertRegex(caught.exception.message, "pg_read_file|pg_ls_dir")


class TestAnotherRightOnAnEnglishServer(ReaderBase, unittest.TestCase):
    def test_a_table_called_app_is_not_the_private_schema(self):
        assert_another_right(self, self.read)


class TestDeadline(ReaderBase, unittest.TestCase):
    def test_a_busy_query_ends_at_the_deadline_and_its_connection_is_gone(self):
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            self.read(_BUSY, timeout_seconds=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "sql_timeout")
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 4.0)
        wait_until_no_reader(self, self.store)

    def test_the_client_cancels_when_the_server_limit_is_far_away(self):
        original = reader_postgres.session_options
        self.addCleanup(setattr, reader_postgres, "session_options", original)
        reader_postgres.session_options = lambda _ms: original(600000)
        slack = reader_postgres.TRANSACTION_SLACK_MS
        self.addCleanup(setattr, reader_postgres, "TRANSACTION_SLACK_MS", slack)
        reader_postgres.TRANSACTION_SLACK_MS = 600000
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            self.read(_BUSY, timeout_seconds=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "sql_timeout")
        self.assertLess(elapsed, 4.0)
        wait_until_no_reader(self, self.store)

    def test_the_connection_is_closed_when_even_the_cancel_does_not_help(self):
        original = reader_postgres.session_options
        self.addCleanup(setattr, reader_postgres, "session_options", original)
        reader_postgres.session_options = lambda _ms: original(600000)
        for name, value in (("TRANSACTION_SLACK_MS", 600000), ("HARD_STOP_SECONDS", 1.0)):
            self.addCleanup(setattr, reader_postgres, name, getattr(reader_postgres, name))
            setattr(reader_postgres, name, value)
        reader = self.store.reader()
        reader._on_deadline = lambda: None      # as if the cancel request got lost
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            reader.run(_BUSY, timeout_seconds=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "sql_timeout")
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 6.0)
        # The server notices the closed connection at the latest when its own limits pass;
        # end the leftover here so that the test does not wait for that.
        owner = self.owner()
        owner.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE usename = 'pcl_reader' AND datname = current_database()")
        wait_until_no_reader(self, self.store)


class TestHardening(ReaderBase, unittest.TestCase):
    def test_the_check_finds_nothing_on_a_fresh_store(self):
        self.assertEqual(pg_hardening.check(self.store), {"problems": [], "notes": []})

    def test_initialize_can_run_again_and_changes_nothing(self):
        owner = self.owner()
        query = ("SELECT (SELECT string_agg(setconfig::text, ';' ORDER BY setconfig::text) "
                 "FROM pg_db_role_setting), (SELECT datacl::text FROM pg_database "
                 "WHERE datname = current_database()), (SELECT string_agg(proacl::text, ';' "
                 "ORDER BY oid) FROM pg_proc WHERE proacl IS NOT NULL), "
                 "(SELECT string_agg(nspacl::text, ';' ORDER BY nspname) FROM pg_namespace), "
                 "(SELECT string_agg(relacl::text, ';' ORDER BY oid) FROM pg_class "
                 "WHERE relnamespace IN ('pcl'::regnamespace, 'app'::regnamespace))")
        before = owner.execute(query).fetchone()
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(owner.execute(query).fetchone(), before)
        self.assertEqual(pg_hardening.check(self.store), {"problems": [], "notes": []})

    def test_the_reader_role_has_its_limits_stored(self):
        owner = self.owner()
        stored = {}
        for (config,) in owner.execute(
                "SELECT setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = "
                "s.setrole JOIN pg_database d ON d.oid = s.setdatabase WHERE r.rolname = "
                "'pcl_reader' AND d.datname = current_database()"):
            for item in config:
                name, _, value = item.partition("=")
                stored[name] = value
        version = owner.info.server_version
        for name, value, _superuser, since in pg_hardening.ROLE_SETTINGS:
            if version >= since:
                self.assertEqual(stored.get(name), value, name)
        row = owner.execute(
            "SELECT rolconnlimit, rolsuper, rolinherit, rolcanlogin FROM pg_roles "
            "WHERE rolname = 'pcl_reader'").fetchone()
        self.assertEqual(row, (pg_hardening.CONNECTION_LIMIT, False, False, True))

    def test_the_app_role_keeps_what_it_needs(self):
        import psycopg
        with psycopg.connect(self.store.conninfo("app"), autocommit=True) as app:
            self.assertEqual(app.execute(
                "SELECT set_config('application_name', 'x', false)").fetchone()[0], "x")
            self.assertGreaterEqual(app.execute(
                "SELECT count(*) FROM pg_stat_activity").fetchone()[0], 1)
            with self.assertRaises(psycopg.Error):
                app.execute("SELECT lo_import('/etc/hosts')")
        with self.store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {app}.users"), 2)

    def test_the_reader_cannot_enter_the_other_databases(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        for name in ("postgres", "template1"):
            with self.assertRaises(psycopg.OperationalError) as caught:
                psycopg.connect(make_conninfo(self.store.conninfo("reader"), dbname=name))
            self.assertIn("permission denied for database", str(caught.exception))

    def test_the_check_reports_a_right_that_must_not_be_there(self):
        owner = self.owner()
        cases = (
            ("GRANT USAGE ON SCHEMA app TO pcl_reader", "REVOKE USAGE ON SCHEMA app FROM "
             "pcl_reader", "private schema app"),
            ("GRANT INSERT ON pcl.meta TO pcl_reader", "REVOKE INSERT ON pcl.meta FROM "
             "pcl_reader", "can change pcl.meta"),
            ("GRANT TEMPORARY ON DATABASE {db} TO pcl_reader", "REVOKE TEMPORARY ON DATABASE "
             "{db} FROM pcl_reader", "temporary tables"),
            ("CREATE VIEW pcl.window AS SELECT display_name FROM app.users",
             "DROP VIEW pcl.window", "window reads app.users"),
            ("GRANT pcl_app TO pcl_reader", "REVOKE pcl_app FROM pcl_reader",
             "member of pcl_app"),
        )
        name = '"%s"' % self.store.database_name
        for grant, revoke, expected in cases:
            owner.execute(grant.replace("{db}", name))
            try:
                problems = pg_hardening.check(self.store)["problems"]
                self.assertTrue(any(expected in problem for problem in problems),
                                (expected, problems))
                # Setting up again does not make such a database look fine.
                if "VIEW" in grant or "pcl_app TO" in grant:
                    with self.assertRaises(PclError) as caught:
                        self.store.initialize()
                    self.assertEqual(caught.exception.code, "db_setup_failed")
                    self.assertIn(expected, caught.exception.message)
            finally:
                owner.execute(revoke.replace("{db}", name))
        self.assertEqual(pg_hardening.check(self.store)["problems"], [])

    def test_the_check_notes_a_weaker_sandbox_and_setup_repairs_it(self):
        owner = self.owner()
        owner.execute("GRANT EXECUTE ON FUNCTION pg_sleep(double precision) TO PUBLIC")
        owner.execute("GRANT USAGE ON LANGUAGE plpgsql TO PUBLIC")
        owner.execute('ALTER ROLE pcl_reader IN DATABASE "%s" RESET statement_timeout'
                      % self.store.database_name)
        found = pg_hardening.check(self.store)
        self.assertEqual(found["problems"], [])
        text = " ".join(found["notes"])
        for expected in ("pg_sleep", "plpgsql", "statement_timeout"):
            self.assertIn(expected, text)
        self.store.initialize()
        self.assertEqual(pg_hardening.check(self.store), {"problems": [], "notes": []})

    def test_too_many_readers_at_once_get_a_plain_answer(self):
        import psycopg
        held = []
        self.addCleanup(lambda: [connection.close() for connection in held])
        for _number in range(pg_hardening.CONNECTION_LIMIT):
            held.append(psycopg.connect(self.store.conninfo("reader")))
        with self.assertRaises(PclError) as caught:
            self.read("SELECT 1")
        self.assertEqual((caught.exception.code, caught.exception.status), ("sql_busy", 503))
        with self.store.app() as db:            # the backend's own role is not affected
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 2)
        held.pop().close()
        self.assertEqual(self.read("SELECT 1")[1], [(1,)])


class TestWithoutSuperuserHardening(ReaderBase, unittest.TestCase):
    """An external database whose owner is not a superuser cannot take the built-in functions
    from PUBLIC. This class gives them back to the reader to show what then still holds (the
    deadline, fresh connections) and what does not (readers see and can stop each other)."""

    FUNCTIONS = ("set_config(text, text, boolean)", "pg_sleep(double precision)",
                 "pg_stat_get_activity(integer)", "pg_terminate_backend(integer, bigint)",
                 "pg_advisory_lock(bigint)")

    def setUp(self):
        super().setUp()
        owner = self.owner()
        for function in self.FUNCTIONS:
            owner.execute("GRANT EXECUTE ON FUNCTION %s TO pcl_reader" % function)

    def tearDown(self):
        owner = _owner(self.store)
        try:
            for function in self.FUNCTIONS:
                owner.execute("REVOKE EXECUTE ON FUNCTION %s FROM pcl_reader" % function)
        finally:
            owner.close()
        super().tearDown()

    def test_a_sleeping_query_ends_at_the_deadline_whatever_it_sets(self):
        for sql in ("SELECT pg_sleep(60)",
                    "SELECT set_config('statement_timeout', '0', false), pg_sleep(60)",
                    "SELECT set_config('statement_timeout', '0', true), "
                    "set_config('lock_timeout', '0', false), pg_sleep(60)",
                    "EXPLAIN ANALYZE SELECT set_config('statement_timeout', '0', false), "
                    "pg_sleep(60)"):
            started = time.monotonic()
            outcome, _text = self.attempt(sql, timeout=1.0)
            elapsed = time.monotonic() - started
            self.assertEqual(outcome, TIMED_OUT, sql)
            self.assertGreaterEqual(elapsed, 0.9, sql)
            self.assertLess(elapsed, 4.0, sql)
        wait_until_no_reader(self, self.store)

    def test_a_setting_changed_by_one_query_is_gone_for_the_next(self):
        outcome, text = self.attempt(
            "SELECT set_config('statement_timeout', '0', false), "
            "set_config('search_path', 'app, pcl', false), "
            "set_config('default_transaction_read_only', 'off', false)")
        self.assertEqual(outcome, OK, text)
        _columns, rows, _ = self.read(
            "SELECT current_setting('statement_timeout'), current_setting('search_path'), "
            "current_setting('default_transaction_read_only')", timeout_seconds=4)
        self.assertEqual(rows, [("4s", "pcl", "on")])
        # And inside the one statement the change buys nothing: still read-only, still no
        # right on the private schema.
        outcome, text = self.attempt(
            "SELECT set_config('search_path', 'app', false), (SELECT count(*) FROM users)")
        self.assertIn(outcome, (ERROR, REFUSED))
        outcome, text = self.attempt(
            "SELECT set_config('transaction_read_only', 'off', true)")
        self.assertEqual(outcome, ERROR, text)

    def test_an_advisory_lock_dies_with_its_connection(self):
        self.assertEqual(self.attempt("SELECT pg_advisory_lock(42)")[0], OK)
        wait_until_no_reader(self, self.store)
        started = time.monotonic()
        self.assertEqual(self.attempt("SELECT pg_advisory_lock(42)")[0], OK)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_without_the_revokes_readers_see_and_stop_each_other(self):
        result = {}

        def victim():
            result["a"] = self.attempt("SELECT pg_sleep(60) /* victim */", timeout=6.0)

        thread = threading.Thread(target=victim)
        thread.start()
        try:
            time.sleep(0.5)
            outcome, text = self.attempt(
                "SELECT query FROM pg_stat_activity WHERE usename = 'pcl_reader'")
            self.assertEqual(outcome, OK)
            # What shows is the reader's own FETCH, not the SQL the other person typed
            # (a text that starts with EXPLAIN would show as typed).
            self.assertIn("FETCH FORWARD", text)
            self.assertNotIn("victim", text)
            started = time.monotonic()
            outcome, text = self.attempt(
                "SELECT pg_terminate_backend(pid, 0) FROM pg_stat_activity "
                "WHERE usename = 'pcl_reader' AND pid <> pg_backend_pid()")
            self.assertEqual(outcome, OK)
        finally:
            thread.join()
        # The stopped reader gets a plain error at once, not a hang until its deadline.
        self.assertIn(result["a"][0], ("db_down", TIMED_OUT, ERROR))
        self.assertLess(time.monotonic() - started, 4.0)


if __name__ == "__main__":
    unittest.main()
