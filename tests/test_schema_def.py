"""schema_def.py: the tables and views of specification section 4.3, and their DDL on both
engines. The views are compared engine against engine on the same rows."""

from __future__ import annotations

import sqlite3
import unittest

from pcldb import util
from pcldb.store import open_store, schema_def
from tests import support

# Specification section 4.3, written out again: "name:kind", a trailing ? = may be empty.
SPEC_TABLES = {
    "pcl.meta": (("key",), "key:text value:text?"),
    "pcl.sessions": (("path",), """
        path:text instrument:text area:text folder:text session_name:text? session_id:text?
        is_copy:bool started_utc:utc? ended_utc:utc? simulated:bool sample_ids:strlist
        operator:text? location:text? method:text? items:int? items_planned:int? completion:text?
        notes:text? tags:strlist format:text? uploaded_utc:utc? upload_id:text? has_card:bool
        card_json:json? corrections_json:json corrected:bool withdrawn:bool bytes:bigint
        n_files:int sums_sha256:text? verified_utc:utc? verify_ok:bool? upload_json:json?
        collection_id:text data_path:text storage_path:text globus_url:text web_url:text
        subject:text registered_utc:utc registered_by:text? updated_utc:utc source:text
        doc_sha256:text?"""),
    "pcl.files": (("session_path", "rel_path"),
                  "session_path:text rel_path:text bytes:bigint sha256:text? listed:bool"),
    "pcl.runs": (("path",), """
        path:text instrument:text area:text folder:text kind:text? made_utc:utc?
        session_path:text? stated_session_path:text? session_id:text? sums_sha256:text?
        derived_from:json tool_name:text? tool_version:text? model_name:text? model_sha256:text?
        runner:text? slurm_job_id:text? run_by:text? set_json:json? why:text? run_json:json?
        valid:bool"""),
    "pcl.points": (("run_path", "point_file"), """
        run_path:text point_file:text point_id:int capture_id:text? label:text? top_class:text?
        confidence:real? outcome:text? needs_review:bool threshold:real? reason:text?
        raw_sha256:text? scores_json:json?"""),
    "pcl.reviews": (("run_path", "row_no"), """
        run_path:text row_no:int reviewed_run:text? point_id:int capture_id:text? status:text
        label:text? reviewer:text? time_utc:utc comment:text?"""),
    "pcl.models": (("name",), """
        name:text path:text sha256:text? classes_json:json? threshold:real? files_json:json?
        is_current:bool model_json:json?"""),
    "pcl.problems": (("problem_no",), """
        problem_no:int path:text code:text detail:text? fix:text? session_path:text?"""),
    "pcl.session_rollup": (("session_path",), """
        session_path:text labels:strlist label_counts:json models:strlist problems:strlist
        review_state:text n_needs_review:int last_run_utc:utc?"""),
    "app.meta": (("key",), "key:text value:text?"),
    "app.users": (("user_id",), """
        user_id:text name:text display_name:text? role:text disabled:bool created_utc:utc"""),
    "app.tokens": (("token_id",), """
        token_id:text user_id:text token_sha256:text label:text? created_utc:utc
        expires_utc:utc? revoked_utc:utc? last_used_utc:utc?"""),
    "app.registrations": (("reg_id",), """
        reg_id:text at_utc:utc user_name:text? session_path:text action:text source:text
        doc_sha256:text? detail_json:json?"""),
    "app.query_log": (("log_id",), """
        log_id:text at_utc:utc user_name:text? mode:text query_text:text request_json:json?
        ok:bool error_text:text? n_rows:int? elapsed_ms:int? backend:text?"""),
    "app.search_entries": (("subject",), """
        subject:text session_path:text content_json:json visible_to:strlist content_sha256:text
        state:text task_id:text? last_error:text? updated_utc:utc synced_utc:utc?"""),
}
SPEC_UNIQUE = {"pcl.sessions": (("subject",),), "app.users": (("name",),),
               "app.tokens": (("token_sha256",),), "app.search_entries": (("session_path",),)}
# The proof of concept's indexes on tables that still exist, plus the two the specification adds.
SPEC_INDEXES = {
    "runs_session": ("pcl.runs", ("session_path", "kind", "model_name")),
    "points_run": ("pcl.points", ("run_path", "point_id")),
    "reviews_run": ("pcl.reviews", ("reviewed_run", "point_id")),
    "files_sha256": ("pcl.files", ("sha256",)),
    "sessions_started": ("pcl.sessions", ("started_utc",)),
    "sessions_instrument": ("pcl.sessions", ("instrument",)),
    "points_label": ("pcl.points", ("label",)),
}
SPEC_VIEWS = {
    "pcl.session_samples": "session_path:text sample_id:text",
    "pcl.session_tags": "session_path:text tag:text",
    "pcl.session_labels": "session_path:text label:text n:int",
    "pcl.current_runs": """path:text instrument:text area:text session_path:text made_utc:utc
        model_name:text model_sha256:text""",
    "pcl.latest_reviews": """run_path:text row_no:int reviewed_run:text point_id:int
        capture_id:text status:text label:text reviewer:text time_utc:utc comment:text""",
    "pcl.current_points": """session_path:text instrument:text simulated:bool withdrawn:bool
        is_copy:bool run_path:text run_made_utc:utc model_name:text point_file:text point_id:int
        capture_id:text model_label:text top_class:text confidence:real outcome:text
        needs_review:bool threshold:real reason:text raw_sha256:text scores_json:json
        review_status:text review_label:text reviewer:text review_time_utc:utc review_run:text
        effective_label:text awaiting_review:bool""",
    "pcl.session_summary": """instrument:text folder:text session_id:text path:text method:text
        completion:text format:text location:text operator:text review_state:text notes:text
        sample_ids:strlist tags:strlist labels:strlist label_counts:json models:strlist
        problems:strlist simulated:bool is_copy:bool withdrawn:bool corrected:bool items:int
        items_planned:int files:int bytes:bigint n_needs_review:int started:utc uploaded:utc
        verified:utc last_run:utc""",
}
# pcl_index/pclindex/schema_sql.py SUMMARY_COLUMNS, as a fixed vector.
POC_SUMMARY_COLUMNS = (
    "instrument", "folder", "session_id", "path", "method", "completion", "format", "location",
    "operator", "review_state", "notes", "sample_ids", "tags", "labels", "label_counts", "models",
    "problems", "simulated", "is_copy", "withdrawn", "corrected", "items", "items_planned", "files",
    "bytes", "n_needs_review", "started", "uploaded", "verified", "last_run")


def parse(text):
    """[(name, kind, nullable)] from the compact notation above."""
    columns = []
    for item in text.split():
        name, _, kind = item.partition(":")
        columns.append((name, kind.rstrip("?"), kind.endswith("?")))
    return columns


class TestDefinition(unittest.TestCase):
    def test_every_table_of_the_specification_with_kinds_keys_and_unique_sets(self):
        self.assertEqual([table.qualified for table in schema_def.TABLES], list(SPEC_TABLES))
        for name, (key, columns) in SPEC_TABLES.items():
            table = schema_def.table(name)
            self.assertEqual([(c.name, c.kind, c.nullable) for c in table.columns], parse(columns),
                             name)
            self.assertEqual(table.primary_key, key, name)
            self.assertEqual(table.unique, SPEC_UNIQUE.get(name, ()), name)
            for column in table.primary_key:
                self.assertFalse(table.columns[table.column_names.index(column)].nullable)

    def test_every_view_of_the_specification_with_columns_and_kinds(self):
        self.assertEqual(sorted(view.qualified for view in schema_def.VIEWS), sorted(SPEC_VIEWS))
        for name, columns in SPEC_VIEWS.items():
            view = schema_def.view(name)
            self.assertEqual([(c.name, c.kind) for c in view.columns],
                             [(n, k) for n, k, _ in parse(columns)], name)
            self.assertEqual(sorted(view.select), ["postgresql", "sqlite"])
        self.assertEqual(schema_def.SUMMARY_COLUMNS, POC_SUMMARY_COLUMNS)
        self.assertEqual(schema_def.view("pcl.session_summary").column_names, POC_SUMMARY_COLUMNS)

    def test_the_indexes_of_the_specification(self):
        found = {index.name: (table.qualified, index.columns)
                 for table in schema_def.TABLES for index in table.indexes}
        for name, expected in SPEC_INDEXES.items():
            self.assertEqual(found.get(name), expected, name)
        self.assertEqual(len(found), sum(len(table.indexes) for table in schema_def.TABLES),
                         "an index name is used twice")
        for table in schema_def.TABLES:
            for index in table.indexes:
                self.assertTrue(set(index.columns) <= set(table.column_names), index.name)

    def test_everything_has_a_one_sentence_description(self):
        for relation in schema_def.relations():
            texts = [(relation.qualified, relation.description)]
            texts += [("%s.%s" % (relation.qualified, c.name), c.description)
                      for c in relation.columns]
            for where, text in texts:
                self.assertIsInstance(text, str, where)
                self.assertGreaterEqual(len(text), 12, where)
                self.assertLessEqual(len(text), 160, where)
                self.assertTrue(text[0].isupper(), where)
                self.assertTrue(text.endswith("."), where)
                self.assertNotIn(". ", text, "%s: more than one sentence" % where)
                self.assertNotIn("\n", text, where)
                text.encode("ascii")        # plain words, plain letters

    def test_one_column_name_never_has_two_kinds(self):
        seen = {}
        for relation in schema_def.TABLES + schema_def.VIEWS:
            for column in relation.columns:
                self.assertIn(column.kind, schema_def.KINDS)
                previous = seen.setdefault(column.name, (column.kind, relation.qualified))
                self.assertEqual(previous[0], column.kind,
                                 "%s is %s in %s but %s in %s" % (
                                     column.name, previous[0], previous[1], column.kind,
                                     relation.qualified))
        self.assertEqual(schema_def.COLUMN_KINDS, {name: kind for name, (kind, _) in seen.items()})
        # A few that results are normalised by.
        for name, kind in (("is_copy", "bool"), ("awaiting_review", "bool"), ("bytes", "bigint"),
                           ("started", "utc"), ("label_counts", "json"), ("labels", "strlist"),
                           ("files", "int"), ("n", "int"), ("confidence", "real")):
            self.assertEqual(schema_def.COLUMN_KINDS[name], kind)

    def test_lookups(self):
        self.assertIs(schema_def.table("pcl.sessions"), schema_def.table("{pcl}.sessions"))
        self.assertEqual(schema_def.table("app.users").kinds["disabled"], "bool")
        self.assertEqual(schema_def.table("pcl.files").column_names,
                         ("session_path", "rel_path", "bytes", "sha256", "listed"))
        self.assertFalse(schema_def.table("pcl.files").is_view)
        self.assertTrue(schema_def.view("pcl.current_runs").is_view)
        for bad in ("sessions", "pcl.nothing", "other.sessions", "pcl.current_runs", "pcl."):
            with self.assertRaises(ValueError, msg=bad):
                schema_def.table(bad)
        with self.assertRaises(ValueError):
            schema_def.view("pcl.sessions")
        self.assertEqual([item.qualified for item in schema_def.relations("app")],
                         [name for name in SPEC_TABLES if name.startswith("app.")])
        self.assertEqual(len(schema_def.relations("pcl")), 9 + 7)
        self.assertEqual(len(schema_def.relations()), 15 + 7)
        self.assertEqual(schema_def.type_name("postgresql", "utc"), "timestamp with time zone")
        self.assertEqual(schema_def.type_name("sqlite", "bool"), "INTEGER (0 or 1)")
        self.assertEqual(schema_def.SCHEMA_VERSION, 1)
        with self.assertRaises(ValueError):
            schema_def.ddl("mysql")
        with self.assertRaises(ValueError):
            schema_def.ddl("sqlite", "public")

    def test_the_four_carried_over_views_are_one_text_for_both_engines(self):
        for name in ("current_runs", "latest_reviews", "current_points", "session_summary"):
            select = schema_def.view("pcl." + name).select
            same = select["postgresql"].replace("pcl.", "").replace(' COLLATE "C"', "")
            self.assertEqual(same, select["sqlite"], name)
            self.assertNotIn("pcl.", select["sqlite"])
            self.assertNotIn("= 1", select["sqlite"])       # booleans are written natively
            self.assertNotIn("= 0", select["sqlite"])

    def test_tie_breaks_on_names_use_the_c_collation_on_postgresql(self):
        runs = schema_def.view("pcl.current_runs").select["postgresql"]
        reviews = schema_def.view("pcl.latest_reviews").select["postgresql"]
        self.assertIn('n.path COLLATE "C" > r.path COLLATE "C"', runs)
        self.assertIn('w.run_path COLLATE "C" > v.run_path COLLATE "C"', reviews)
        for types in (schema_def.SQL_TYPES["postgresql"],):
            self.assertIn('COLLATE "C"', types["text"])
            self.assertIn('COLLATE "C"', types["strlist"])


class TestSchemaFiles(unittest.TestCase):
    """pcldb/schemas holds byte-for-byte copies of the proof of concept's two files. Their
    SHA-256 is pinned here: the original folder is not part of this repository."""

    PINNED = {
        "pcl_session.schema.json":
            "ea0307f86410aeb55760bb36bcfe5a9a58c533f6b742d593d9fcc33d9d0c5508",
        "search_fields.json":
            "291d03b0cf0fb1276892bcac1430fdf29152f20dee5d3524a7a38a7aeb86d002",
    }

    def test_the_two_files_are_the_pinned_copies(self):
        import json
        from pathlib import Path
        folder = Path(schema_def.__file__).resolve().parent.parent / "schemas"
        self.assertEqual(sorted(path.name for path in folder.iterdir()), sorted(self.PINNED))
        for name, digest in self.PINNED.items():
            raw = (folder / name).read_bytes()
            self.assertEqual(util.sha256_hex(raw), digest, name)
            self.assertNotIn(b"\r", raw, name)
            json.loads(raw.decode("utf-8"))
        card = json.loads((folder / "pcl_session.schema.json").read_text(encoding="utf-8"))
        fields = json.loads((folder / "search_fields.json").read_text(encoding="utf-8"))
        self.assertEqual(card["$id"], "pcl-session/1")
        self.assertEqual(fields["schema"], "pcl-search-fields/1")
        # The Search fields that are columns of session_summary have matching kinds.
        kinds = {"string": ("text",), "text": ("text",), "string_list": ("strlist",),
                 "object_list": ("json",), "boolean": ("bool",), "integer": ("int", "bigint"),
                 "date": ("utc",)}
        summary = schema_def.view("pcl.session_summary").kinds
        for name, spec in fields["fields"].items():
            if name in summary:
                self.assertIn(summary[name], kinds[spec["type"]], name)
        self.assertEqual(sorted(set(fields["fields"]) - set(summary)),
                         ["collection_id", "data_path", "globus_url", "schema", "title",
                          "web_url"])


class TestSqliteDdl(unittest.TestCase):
    def build(self, statements, attach):
        connection = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(connection.close)
        if attach:
            connection.execute("ATTACH DATABASE ':memory:' AS pcl")
        for statement in statements:
            connection.execute(statement)
        return connection

    def test_it_executes_cleanly_on_an_empty_database(self):
        connection = self.build(schema_def.ddl("sqlite"), attach=True)
        for schema, name in (("pcl", "pcl"), ("app", "main")):
            made = {row[0]: row[1] for row in connection.execute(
                "SELECT name, type FROM %s.sqlite_master WHERE name NOT LIKE 'sqlite_%%'" % name)}
            for relation in schema_def.relations(schema):
                self.assertEqual(made.pop(relation.name), "view" if relation.is_view else "table")
            for table in schema_def.TABLES:
                if table.schema == schema:
                    for index in table.indexes:
                        self.assertEqual(made.pop(index.name), "index")
            self.assertEqual(made, {})

    def test_column_types_and_not_null(self):
        connection = self.build(schema_def.ddl("sqlite"), attach=True)
        expected = {"text": "TEXT", "int": "INTEGER", "bigint": "INTEGER", "real": "REAL",
                    "bool": "INTEGER", "utc": "TEXT", "json": "TEXT", "strlist": "TEXT"}
        for table in schema_def.TABLES:
            schema = schema_def.SCHEMA_NAMES["sqlite"][table.schema]
            info = connection.execute("PRAGMA %s.table_info(%s)" % (schema, table.name)).fetchall()
            self.assertEqual([(row[1], row[2], bool(row[3])) for row in info],
                             [(c.name, expected[c.kind], not c.nullable) for c in table.columns])
            key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            self.assertEqual(tuple(key), table.primary_key)

    def test_view_columns_are_the_defined_ones(self):
        connection = self.build(schema_def.ddl("sqlite"), attach=True)
        for view in schema_def.VIEWS:
            cursor = connection.execute("SELECT * FROM pcl.%s LIMIT 0" % view.name)
            self.assertEqual(tuple(item[0] for item in cursor.description), view.column_names)

    def test_the_catalogue_can_be_built_alone_and_is_the_same(self):
        attached = self.build(schema_def.ddl("sqlite"), attach=True)
        alone = self.build(schema_def.ddl("sqlite", "pcl", qualified=False), attach=False)
        query = "SELECT type, name, tbl_name, sql FROM %ssqlite_master ORDER BY name"
        self.assertEqual(alone.execute(query % "").fetchall(),
                         attached.execute(query % "pcl.").fetchall())
        for statement in schema_def.ddl("sqlite", "pcl", qualified=False):
            self.assertNotIn("pcl.", statement)
        only_app = self.build(schema_def.ddl("sqlite", "app", qualified=False), attach=False)
        names = {row[0] for row in only_app.execute("SELECT name FROM sqlite_master")}
        self.assertIn("users", names)
        self.assertNotIn("sessions", names)


class TestPostgresDdl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = support.shared_cluster()
        cls.database = cls.cluster.new_database()
        cls.addClassCleanup(cls.cluster.drop_database, cls.database)
        import psycopg
        cls.connection = psycopg.connect(cls.cluster.conninfo("owner", cls.database),
                                         autocommit=True)
        cls.addClassCleanup(cls.connection.close)
        for statement in schema_def.ddl("postgresql"):      # an empty database: must all run
            cls.connection.execute(statement)

    def rows(self, sql, params=None):
        return self.connection.execute(sql, params).fetchall()

    def test_every_relation_was_made(self):
        made = {(schema, name): kind for schema, name, kind in self.rows(
            "SELECT n.nspname, c.relname, c.relkind FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('pcl', 'app') AND c.relkind IN ('r', 'v')")}
        self.assertEqual(made, {(item.schema, item.name): "v" if item.is_view else "r"
                                for item in schema_def.relations()})

    def test_column_types_collation_and_not_null(self):
        expected = {"text": "text", "int": "integer", "bigint": "bigint",
                    "real": "double precision", "bool": "boolean",
                    "utc": "timestamp with time zone", "json": "jsonb", "strlist": "ARRAY"}
        for table in schema_def.TABLES:
            found = self.rows(
                "SELECT column_name, data_type, is_nullable, collation_name "
                "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position", [table.schema, table.name])
            self.assertEqual([(row[0], row[1], row[2] == "YES") for row in found],
                             [(c.name, expected[c.kind], c.nullable) for c in table.columns],
                             table.qualified)
            for column, row in zip(table.columns, found):
                if column.kind in ("text", "strlist"):
                    self.assertEqual(row[3], "C", "%s.%s" % (table.qualified, column.name))

    def test_view_columns_are_the_defined_ones(self):
        for view in schema_def.VIEWS:
            cursor = self.connection.execute("SELECT * FROM pcl.%s LIMIT 0" % view.name)
            self.assertEqual(tuple(item.name for item in cursor.description), view.column_names)

    def test_keys_unique_sets_and_indexes(self):
        for table in schema_def.TABLES:
            constraints = self.rows(
                "SELECT c.contype, array_agg(a.attname ORDER BY k.position) "
                "FROM pg_constraint c "
                "CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, position) "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                "WHERE c.conrelid = %s::regclass AND c.contype IN ('p', 'u') "
                "GROUP BY c.oid, c.contype", [table.qualified])
            self.assertEqual(sorted((kind, tuple(columns)) for kind, columns in constraints),
                             sorted([("p", table.primary_key)]
                                    + [("u", columns) for columns in table.unique]),
                             table.qualified)
            indexes = {row[0] for row in self.rows(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
                [table.schema, table.name])}
            for index in table.indexes:
                self.assertIn(index.name, indexes)

    def test_descriptions_are_applied_as_comments(self):
        self.assertEqual(self.rows("SELECT obj_description('pcl'::regnamespace)")[0][0],
                         schema_def.SCHEMA_DESCRIPTIONS["pcl"])
        for relation in schema_def.relations():
            self.assertEqual(
                self.rows("SELECT obj_description(%s::regclass)", [relation.qualified])[0][0],
                relation.description, relation.qualified)
            comments = dict(self.rows(
                "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
                "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
                [relation.qualified]))
            self.assertEqual(comments, {c.name: c.description for c in relation.columns},
                             relation.qualified)


# ---------------------------------------------------------------------- the views, both engines

T0, T1, T2, T3, T4 = ("2026-11-18T10:00:00.000Z", "2026-11-19T02:31:50.210Z",
                      "2026-11-19T03:00:00.000Z", "2026-11-19T04:00:00.000Z",
                      "2026-11-19T05:00:00.5Z")
S1, S2, S3, S4 = ("raw/raman-portable/scan_1", "raw/raman-portable/simulated/scan_2",
                  "raw/jeol-jsm-it800-shl/sem_3", "raw/raman-portable/Scan_\u00e9_4")
SHA_A, SHA_B = "aa" * 32, "bb" * 32


def run_row(path, session, model="m1", sha=SHA_A, made=T1, kind="classify", valid=True):
    return {"path": path, "instrument": "raman-portable", "area": "real",
            "folder": path.rsplit("/", 1)[-1], "kind": kind, "made_utc": made,
            "session_path": session, "stated_session_path": session, "derived_from": [],
            "model_name": model, "model_sha256": sha, "valid": valid}


def point_row(run, number, label, needs_review=False, confidence=0.9):
    return {"run_path": run, "point_file": "point_%04d.json" % number, "point_id": number,
            "capture_id": "c-%d" % number, "label": label, "top_class": label,
            "confidence": confidence, "outcome": "confident", "needs_review": needs_review,
            "threshold": 0.8, "scores_json": {label or "none": confidence}}


def review_row(run, row_no, reviewed, point, status, label=None, time=T2):
    return {"run_path": run, "row_no": row_no, "reviewed_run": reviewed, "point_id": point,
            "status": status, "label": label, "reviewer": "demo-reviewer", "time_utc": time,
            "comment": None}


def fill(db):
    """Rows that exercise every rule of the views, ties and missing values included."""
    db.upsert("pcl.meta", {"key": "current_model", "value": "m1"}, key=("key",))
    for name, sha, current in (("m1", SHA_A, True), ("m2", SHA_B, False)):
        db.insert("pcl.models", {"name": name, "path": "models/" + name, "sha256": sha,
                                 "is_current": current})
    for path, simulated, extra in (
            (S1, False, {"verify_ok": True, "verified_utc": T2, "sample_ids": ["S-1", "S-2"],
                         "tags": ["keep", "demo"]}),
            (S2, True, {"verify_ok": False, "verified_utc": T2, "sample_ids": [],
                        "withdrawn": True}),
            (S3, False, {"verify_ok": None, "verified_utc": None, "sample_ids": ["S-2"],
                         "started_utc": None, "is_copy": True}),
            (S4, False, {"sample_ids": ["\u00e9chantillon 7"], "tags": ["x"], "corrected": True})):
        db.insert("pcl.sessions", support.session_row(path, simulated, **extra))
    db.insert_many("pcl.runs", [
        # Two runs of the same moment: the greater path wins, and "a" is greater than "B" in
        # byte order (a language collation would say the opposite).
        run_row("results/x/a_run", S1),
        run_row("results/x/B_run", S1),
        run_row("results/x/old_run", S1, made=T0),
        run_row("results/x/invalid_newer", S1, made=T3, valid=False),
        run_row("results/x/other_model", S1, model="m2", sha=SHA_B, made=T3),
        run_row("results/x/changed_model_files", S1, sha="cc" * 32, made=T4),
        run_row("results/x/s2_run", S2),
        run_row("results/x/no_session", None, made=T4),
        run_row("results/x/no_time_1", S4, made=None),
        run_row("results/x/no_time_2", S4, made=None),
        run_row("results/x/review_b", S1, kind="review", model=None, sha=None, made=T2),
        run_row("results/x/Review_A", S1, kind="review", model=None, sha=None, made=T2),
    ])
    db.insert_many("pcl.points", [
        point_row("results/x/a_run", 1, "adenine"),
        point_row("results/x/a_run", 2, "UNKNOWN", needs_review=True, confidence=0.55),
        point_row("results/x/a_run", 3, "UNKNOWN", needs_review=True, confidence=0.61),
        point_row("results/x/a_run", 4, "r6g"),
        point_row("results/x/a_run", 5, None, needs_review=True, confidence=0.1),
        point_row("results/x/B_run", 1, "r6g"),
        point_row("results/x/old_run", 1, "adenine"),
        point_row("results/x/s2_run", 1, "methylene_blue", needs_review=True),
        point_row("results/x/no_time_1", 1, "adenine"),
        point_row("results/x/no_time_2", 1, "r6g"),
    ])
    db.insert_many("pcl.reviews", [
        # Point 1: three decisions; the newest time wins.
        review_row("results/x/review_b", 1, "results/x/a_run", 1, "confirmed", time=T1),
        review_row("results/x/review_b", 2, "results/x/a_run", 1, "rejected", time=T3),
        review_row("results/x/review_b", 3, "results/x/a_run", 1, "confirmed", time=T2),
        # Point 2: the same moment in two review runs; the greater run path wins (byte order:
        # "review_b" is greater than "Review_A").
        review_row("results/x/review_b", 4, "results/x/a_run", 2, "relabelled", "r6g"),
        review_row("results/x/Review_A", 1, "results/x/a_run", 2, "confirmed"),
        # Point 4: the same moment in the same run; the greater row number wins.
        review_row("results/x/Review_A", 2, "results/x/a_run", 4, "confirmed"),
        review_row("results/x/Review_A", 3, "results/x/a_run", 4, "relabelled", "adenine"),
        # A decision about a run that is not current, and one without a run.
        review_row("results/x/Review_A", 4, "results/x/old_run", 1, "rejected"),
        review_row("results/x/Review_A", 5, None, 9, "confirmed"),
    ])
    db.insert("pcl.session_rollup", {
        "session_path": S1, "labels": ["UNKNOWN", "adenine", "r6g"],
        "label_counts": [{"label": "UNKNOWN", "count": 1}, {"label": "adenine", "count": 1},
                         {"label": "r6g", "count": 12345}, {"label": 5, "count": 1},
                         {"label": "half", "count": 0.5}, "not an object"],
        "models": ["m1", "m2"], "problems": ["card_missing"], "review_state": "pending",
        "n_needs_review": 2, "last_run_utc": T4})
    db.insert("pcl.session_rollup", {
        "session_path": S2, "labels": [], "label_counts": {"not": "a list"}, "models": [],
        "problems": [], "review_state": "none", "n_needs_review": 0, "last_run_utc": None})


def view_rows(store):
    rows = {}
    with store.app() as db:
        for view in schema_def.VIEWS:
            found = db.query("SELECT * FROM {pcl}.%s" % view.name)
            rows[view.name] = sorted(found, key=util.canonical_json)
    return rows


class TestViewsAgree(unittest.TestCase):
    """The same rows go into a SQLite store and a PostgreSQL store; every view must answer the
    same on both."""

    @classmethod
    def setUpClass(cls):
        cls.cluster = support.shared_cluster()

    def sqlite_store(self):
        project = support.TempProject("sqlite")
        self.addCleanup(project.cleanup)
        store = open_store(project.settings)
        store.initialize()
        return store

    def postgres_store(self, create=None):
        """A store on a fresh database. `create` (a function taking a database name and giving
        the CREATE DATABASE statement) makes it with another default collation."""
        from psycopg import sql
        if create is None:
            database = self.cluster.new_database()
        else:
            database = "t_other_" + util.new_id()[:10]
            connection = self.cluster._owner()
            try:
                connection.execute(sql.SQL(create).format(sql.Identifier(database)))
            finally:
                connection.close()
        self.addCleanup(self.cluster.drop_database, database)
        project = support.TempProject("postgresql", self.cluster.external_changes(database))
        self.addCleanup(project.cleanup)
        store = open_store(project.settings)
        store.initialize()
        return store

    def compare(self, postgres_store):
        sqlite_store = self.sqlite_store()
        for store in (sqlite_store, postgres_store):
            with store.app() as db:
                fill(db)
        ours, theirs = view_rows(sqlite_store), view_rows(postgres_store)
        for view in schema_def.VIEWS:
            self.assertEqual(ours[view.name], theirs[view.name], view.name)
        return ours

    def test_every_view_gives_the_same_rows_on_both_engines(self):
        rows = self.compare(self.postgres_store())

        # And the rows are the right ones (so "the same" is not "both empty").
        current = {row["session_path"]: row["path"] for row in rows["current_runs"]
                   if row["session_path"] != S4}
        self.assertEqual(current, {S1: "results/x/a_run", S2: "results/x/s2_run"})
        self.assertEqual(sorted(row["path"] for row in rows["current_runs"]
                                if row["session_path"] == S4),
                         ["results/x/no_time_1", "results/x/no_time_2"])
        latest = {(row["reviewed_run"], row["point_id"]): (row["run_path"], row["row_no"],
                                                           row["status"])
                  for row in rows["latest_reviews"]}
        self.assertEqual(latest, {
            ("results/x/a_run", 1): ("results/x/review_b", 2, "rejected"),
            ("results/x/a_run", 2): ("results/x/review_b", 4, "relabelled"),
            ("results/x/a_run", 4): ("results/x/Review_A", 3, "relabelled"),
            ("results/x/old_run", 1): ("results/x/Review_A", 4, "rejected"),
            (None, 9): ("results/x/Review_A", 5, "confirmed"),
        })
        points = {(row["session_path"], row["point_id"]): row for row in rows["current_points"]
                  if row["session_path"] == S1}
        self.assertEqual(sorted(number for _session, number in points), [1, 2, 3, 4, 5])
        effective = {number: (row["effective_label"], row["awaiting_review"], row["review_status"])
                     for (_session, number), row in points.items()}
        self.assertEqual(effective, {
            1: (None, False, "rejected"),               # rejected: no label counts
            2: ("r6g", False, "relabelled"),            # the reviewer's label
            3: ("UNKNOWN", True, None),                 # needs a review, has none
            4: ("adenine", False, "relabelled"),
            5: (None, True, None),
        })
        self.assertIs(points[(S1, 1)]["simulated"], False)
        self.assertEqual(points[(S1, 2)]["scores_json"], {"UNKNOWN": 0.55})
        self.assertEqual(points[(S1, 2)]["run_made_utc"], T1)
        self.assertEqual(points[(S1, 2)]["review_time_utc"], T2)

        summary = {row["path"]: row for row in rows["session_summary"]}
        self.assertEqual(sorted(summary), sorted([S1, S2, S3, S4]))
        self.assertEqual(tuple(summary[S1]), POC_SUMMARY_COLUMNS)
        self.assertEqual(summary[S1]["verified"], T2)       # the check passed
        self.assertIsNone(summary[S2]["verified"])          # the check failed
        self.assertIsNone(summary[S3]["verified"])          # there was none
        self.assertEqual((summary[S3]["review_state"], summary[S3]["n_needs_review"],
                          summary[S3]["labels"], summary[S3]["started"]), ("none", 0, None, None))
        self.assertEqual(summary[S1]["labels"], ["UNKNOWN", "adenine", "r6g"])
        self.assertEqual(summary[S1]["last_run"], "2026-11-19T05:00:00.500Z")
        self.assertIs(summary[S2]["withdrawn"], True)
        self.assertIs(summary[S3]["is_copy"], True)

        self.assertEqual(sorted((row["session_path"], row["sample_id"])
                                for row in rows["session_samples"]),
                         sorted([(S1, "S-1"), (S1, "S-2"), (S3, "S-2"),
                                 (S4, "\u00e9chantillon 7")]))
        self.assertEqual(sorted((row["session_path"], row["tag"]) for row in rows["session_tags"]),
                         sorted([(S1, "keep"), (S1, "demo"), (S4, "x")]))
        labels = sorted(((row["label"], row["n"]) for row in rows["session_labels"]),
                        key=lambda pair: (pair[0] is None, pair[0] or ""))
        self.assertEqual(labels, [("UNKNOWN", 1), ("adenine", 1), ("half", None), ("r6g", 12345),
                                  (None, 1)])
        self.assertEqual({row["session_path"] for row in rows["session_labels"]}, {S1})

    def test_a_database_with_a_language_collation_still_agrees(self):
        """An external PostgreSQL may sort text by a language's rules ("a" before "B"). The
        views and the text columns say COLLATE "C", so the answers stay the same."""
        import psycopg
        store, problems = None, []
        for create in (
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER icu "
                "ICU_LOCALE 'en-US'",
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE_PROVIDER libc "
                "LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8'"):
            try:
                store = self.postgres_store(create)
                break
            except psycopg.Error as error:
                problems.append(str(error).splitlines()[0])
        if store is None:
            self.skipTest("this PostgreSQL offers neither ICU nor an en_US.UTF-8 locale: "
                          + "; ".join(problems))
        connection = psycopg.connect(store.conninfo("app"))
        try:
            by_language = connection.execute("SELECT 'a' < 'B'").fetchone()[0]
            by_column = connection.execute(
                "SELECT path FROM pcl.runs ORDER BY path LIMIT 1").fetchone()
        finally:
            connection.close()
        self.assertTrue(by_language, "the database was meant to sort by a language's rules")
        self.assertIsNone(by_column)
        rows = self.compare(store)
        current = {row["session_path"]: row["path"] for row in rows["current_runs"]}
        self.assertEqual(current[S1], "results/x/a_run")
        with store.app() as db:
            ordered = [row["path"] for row in db.query(
                "SELECT path FROM {pcl}.runs WHERE path LIKE 'results/x/%_run' ORDER BY path")]
        self.assertEqual(ordered, sorted(ordered))      # byte order, as Python sorts str
        self.assertLess(ordered.index("results/x/B_run"), ordered.index("results/x/a_run"))


if __name__ == "__main__":
    unittest.main()
