"""config.py: defaults, the settings file, the environment, secrets, Settings.for_test."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcldb import config
from pcldb.config import Settings, load_settings
from pcldb.errors import PclError
from tests import support

PROJECT = Path(__file__).resolve().parent.parent

# Section 3 of the specification, written out again so that a changed default is noticed.
SPEC_DEFAULTS = {
    "database": {
        "engine": "postgresql",
        "postgresql": {"mode": "local", "bin_dir": None, "port": 15439, "name": "pcl",
                       "external": {"owner": None, "app": None, "reader": None}},
        "sqlite": {"folder": "var/sqlite"},
    },
    "server": {"host": "127.0.0.1", "port": 8787, "allowed_hosts": []},
    "storage": {"collection_id": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
                "collection_root": "/projects", "project_path": "/projects/pcl_photonics",
                "ssh_host": "datatransfer.arc.vt.edu"},
    "search": {"backend": "local", "index_id": None, "visible_to": [],
               "client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2",
               "include_operator": False, "include_notes": False, "fallback_to_local": True},
    "llm": {"enabled": True, "base_url": "https://llm-api.arc.vt.edu/api/v1",
            "model": "gpt-oss-120b", "reasoning_effort": "low", "response_format": False,
            "max_concurrent": 2, "api_key_file": None, "timeout_seconds": 90},
    "ask": {"include_simulated_default": True, "max_results": 20, "per_user_per_minute": 20},
    "sql": {"max_rows": 1000, "timeout_seconds": 15, "max_cell_chars": 10000,
            "max_sql_chars": 20000},
}


def without_notes(tree):
    return {key: without_notes(value) if isinstance(value, dict) else value
            for key, value in tree.items() if not key.startswith("_")}


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="pcldb config "))
        self.addCleanup(support.remove_tree, self.folder)
        self.project = self.folder / "project"
        self.user = self.folder / "home" / ".pcldb"
        self.project.mkdir()

    def write(self, data, bom=False):
        raw = json.dumps(data).encode("utf-8")
        (self.project / "pcldb_config.json").write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)

    def load(self, env=None):
        return load_settings(self.project, self.user, env=env or {})


class TestDefaultsAndFile(ConfigCase):
    def test_defaults_are_the_specification(self):
        self.assertEqual(config.DEFAULTS, SPEC_DEFAULTS)
        settings = self.load()
        self.assertEqual(settings.to_dict(), SPEC_DEFAULTS)
        self.assertEqual(settings.database.engine, "postgresql")
        self.assertEqual(settings.database.postgresql.port, 15439)
        self.assertEqual(settings.database.postgresql.external.reader, None)
        self.assertEqual(settings.server.allowed_hosts, ())
        self.assertEqual(settings.search.visible_to, ())
        self.assertEqual(settings.sql.max_rows, 1000)
        self.assertEqual(settings.llm.timeout_seconds, 90)
        self.assertEqual(settings.storage.ssh_host, "datatransfer.arc.vt.edu")
        self.assertIsNone(settings.server_url)
        self.assertIsNone(settings.token)

    def test_the_file_wins_over_the_defaults(self):
        self.write({"database": {"engine": "sqlite", "postgresql": {"port": 15999}},
                    "sql": {"max_rows": 7}, "search": {"visible_to": ["urn:globus:groups:id:x"]},
                    "server": {"allowed_hosts": ["lab-pc:8787"]}})
        settings = self.load()
        self.assertEqual(settings.database.engine, "sqlite")
        self.assertEqual(settings.database.postgresql.port, 15999)
        self.assertEqual(settings.database.postgresql.name, "pcl")       # untouched default
        self.assertEqual(settings.sql.max_rows, 7)
        self.assertEqual(settings.sql.timeout_seconds, 15)
        self.assertEqual(settings.search.visible_to, ("urn:globus:groups:id:x",))
        self.assertEqual(settings.server.allowed_hosts, ("lab-pc:8787",))

    def test_a_byte_order_mark_in_the_file_is_accepted(self):
        self.write({"sql": {"max_rows": 9}}, bom=True)
        self.assertEqual(self.load().sql.max_rows, 9)

    def test_an_unknown_key_is_an_error_that_names_it(self):
        for data, key in (({"databse": {}}, "databse"),
                          ({"database": {"postgresql": {"prot": 1}}}, "database.postgresql.prot"),
                          ({"sql": {"max_rows": 5, "maxrows": 5}}, "sql.maxrows"),
                          ({"database": {"postgresql": {"external": {"admin": "x"}}}},
                           "database.postgresql.external.admin")):
            self.write(data)
            with self.assertRaises(PclError, msg=key) as caught:
                self.load()
            self.assertIn(key, str(caught.exception))
            self.assertIn("pcldb_config.json", str(caught.exception))
            self.assertEqual(caught.exception.details, {"key": key})
            self.assertEqual(caught.exception.code, "unknown_setting")

    def test_keys_that_start_with_an_underscore_are_notes_at_every_level(self):
        self.write({"_about": "a note", "_anything": {"x": [1, 2]},
                    "database": {"_about": {"engine": "..."}, "engine": "sqlite",
                                 "postgresql": {"_why": "note", "external": {"_about": 5}}},
                    "sql": {"_about": "note", "max_rows": 3}})
        settings = self.load()
        self.assertEqual(settings.database.engine, "sqlite")
        self.assertEqual(settings.sql.max_rows, 3)
        expected = json.loads(json.dumps(SPEC_DEFAULTS))
        expected["database"]["engine"] = "sqlite"
        expected["sql"]["max_rows"] = 3
        self.assertEqual(settings.to_dict(), expected)

    def test_wrong_values_are_errors_that_name_the_key(self):
        cases = (
            ({"database": {"engine": "mysql"}}, "database.engine"),
            ({"database": {"postgresql": {"port": "15439"}}}, "database.postgresql.port"),
            ({"database": {"postgresql": {"port": 0}}}, "database.postgresql.port"),
            ({"database": {"postgresql": {"port": True}}}, "database.postgresql.port"),
            ({"database": {"postgresql": {"mode": "remote"}}}, "database.postgresql.mode"),
            ({"database": {"postgresql": {"name": "My DB"}}}, "database.postgresql.name"),
            ({"database": "sqlite"}, "database"),
            ({"server": {"allowed_hosts": "localhost"}}, "server.allowed_hosts"),
            ({"search": {"backend": "elastic"}}, "search.backend"),
            ({"search": {"include_notes": 1}}, "search.include_notes"),
            ({"llm": {"base_url": "llm-api.arc.vt.edu"}}, "llm.base_url"),
            ({"llm": {"timeout_seconds": -1}}, "llm.timeout_seconds"),
            ({"sql": {"max_rows": 2.5}}, "sql.max_rows"),
            ({"storage": {"project_path": "projects/pcl"}}, "storage.project_path"),
        )
        for data, key in cases:
            self.write(data)
            with self.assertRaises(PclError, msg=key) as caught:
                self.load()
            self.assertIn(key, str(caught.exception))

    def test_the_project_folder_must_be_inside_the_collection_root(self):
        self.write({"storage": {"collection_root": "/projects", "project_path": "/home/lab"}})
        with self.assertRaises(PclError) as caught:
            self.load()
        self.assertIn("storage.project_path", str(caught.exception))
        self.write({"storage": {"collection_root": "/projects/", "project_path": "/projects/x/"}})
        settings = self.load()
        self.assertEqual((settings.storage.collection_root, settings.storage.project_path),
                         ("/projects", "/projects/x"))

    def test_a_file_that_is_not_json_or_not_an_object_is_refused(self):
        (self.project / "pcldb_config.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(PclError) as caught:
            self.load()
        self.assertIn("pcldb_config.json", str(caught.exception))
        self.write(["database"])
        with self.assertRaises(PclError):
            self.load()

    def test_a_wrong_connection_string_value_is_never_echoed(self):
        self.write({"database": {"postgresql": {"external": {"owner": {"password": "hunter2"}}}}})
        with self.assertRaises(PclError) as caught:
            self.load()
        self.assertIn("database.postgresql.external.owner", str(caught.exception))
        self.assertNotIn("hunter2", str(caught.exception))


class TestExampleFile(unittest.TestCase):
    def setUp(self):
        with open(PROJECT / "pcldb_config.example.json", "r", encoding="utf-8") as handle:
            self.text = handle.read()
        self.example = json.loads(self.text)

    def test_it_holds_every_key_with_its_default(self):
        self.assertEqual(without_notes(self.example), SPEC_DEFAULTS)

    def test_every_key_is_explained_in_the_about_note_beside_it(self):
        def check(tree, where):
            about = tree.get("_about")
            keys = [key for key in tree if not key.startswith("_")]
            self.assertIsInstance(about, dict, "%s has no _about object" % where)
            self.assertEqual(sorted(about), sorted(keys), "the notes of %s" % where)
            for key in keys:
                self.assertTrue(about[key].strip().endswith((".", ")")), (where, key))
                if isinstance(tree[key], dict):
                    check(tree[key], where + "." + key)
        self.assertIsInstance(self.example["_about"], str)
        for section in SPEC_DEFAULTS:
            check(self.example[section], section)

    def test_it_can_be_used_as_the_settings_file(self):
        folder = Path(tempfile.mkdtemp(prefix="pcldb example "))
        self.addCleanup(support.remove_tree, folder)
        (folder / "pcldb_config.json").write_text(self.text, encoding="utf-8")
        loaded = load_settings(folder, folder / "user", env={})
        (folder / "pcldb_config.json").unlink()
        self.assertEqual(loaded, load_settings(folder, folder / "user", env={}))

    def test_it_holds_no_secret_and_is_plain_ascii_with_unix_line_ends(self):
        raw = (PROJECT / "pcldb_config.example.json").read_bytes()
        self.assertNotIn(b"\r", raw)
        raw.decode("ascii")
        for key in ("owner", "app", "reader"):
            self.assertIsNone(self.example["database"]["postgresql"]["external"][key])
        self.assertIsNone(self.example["llm"]["api_key_file"])


class TestEnvironment(ConfigCase):
    def test_the_environment_wins_over_the_file(self):
        self.write({"database": {"engine": "postgresql", "postgresql": {"bin_dir": "/from/file"}},
                    "llm": {"base_url": "https://file.example/api", "model": "file-model"}})
        settings = self.load({
            "PCLDB_ENGINE": "sqlite", "PCLDB_PG_BIN": "/from/env",
            "PCLDB_LLM_BASE_URL": "http://127.0.0.1:1/v1/", "PCLDB_LLM_MODEL": "env-model",
            "PCLDB_SERVER_URL": "http://127.0.0.1:2", "PCLDB_TOKEN": "pcl_from_env"})
        self.assertEqual(settings.database.engine, "sqlite")
        self.assertEqual(settings.database.postgresql.bin_dir, "/from/env")
        self.assertEqual(settings.llm.base_url, "http://127.0.0.1:1/v1")
        self.assertEqual(settings.llm.model, "env-model")
        self.assertEqual(settings.server_url, "http://127.0.0.1:2")
        self.assertEqual(settings.token, "pcl_from_env")

    def test_empty_variables_count_as_not_set(self):
        self.write({"database": {"engine": "sqlite"}})
        settings = self.load({"PCLDB_ENGINE": "", "PCLDB_PG_BIN": "  ", "PCLDB_TOKEN": ""})
        self.assertEqual(settings.database.engine, "sqlite")
        self.assertIsNone(settings.database.postgresql.bin_dir)
        self.assertIsNone(settings.token)

    def test_a_wrong_variable_is_an_error_that_names_it(self):
        with self.assertRaises(PclError) as caught:
            self.load({"PCLDB_ENGINE": "oracle"})
        self.assertIn("PCLDB_ENGINE", str(caught.exception))
        self.assertIn("database.engine", str(caught.exception))

    def test_connection_strings_from_the_environment_mean_external_mode(self):
        self.write({"database": {"postgresql": {"mode": "local",
                                                "external": {"owner": "host=file"}}}})
        self.assertEqual(self.load().database.postgresql.mode, "local")
        settings = self.load({"PCLDB_PG_OWNER_DSN": "host=h user=o password=p1",
                              "PCLDB_PG_APP_DSN": "host=h user=pcl_app password=p2",
                              "PCLDB_PG_READER_DSN": "host=h user=pcl_reader password=p3"})
        self.assertEqual(settings.database.postgresql.mode, "external")
        self.assertEqual(settings.database.postgresql.external.owner, "host=h user=o password=p1")
        self.assertEqual(settings.database.postgresql.external.app,
                         "host=h user=pcl_app password=p2")
        self.assertEqual(settings.database.postgresql.external.reader,
                         "host=h user=pcl_reader password=p3")

    def test_project_and_user_folders_from_the_environment(self):
        other = self.folder / "other project"
        other.mkdir()
        (other / "pcldb_config.json").write_text('{"sql": {"max_rows": 4}}', encoding="utf-8")
        settings = load_settings(env={"PCLDB_DIR": str(other),
                                      "PCLDB_USER_DIR": str(self.folder / "u")})
        self.assertEqual(settings.project_dir, other)
        self.assertEqual(settings.user_dir, self.folder / "u")
        self.assertEqual(settings.sql.max_rows, 4)
        self.assertEqual(settings.config_path, other / "pcldb_config.json")
        # An explicit folder wins over the variable.
        settings = load_settings(self.project, self.user, env={"PCLDB_DIR": str(other)})
        self.assertEqual(settings.project_dir, self.project)
        self.assertEqual(settings.sql.max_rows, 1000)

    def test_without_anything_the_project_is_the_folder_of_the_package(self):
        self.assertEqual(config.default_project_dir(), PROJECT)
        # (The real project's own settings file is not read by a test: a stand-in folder.)
        self.write({"sql": {"max_rows": 6}})
        with mock.patch.object(config, "default_project_dir", return_value=self.project):
            settings = load_settings(user_dir=self.user, env={})
        self.assertEqual(settings.project_dir, self.project)
        self.assertEqual(settings.sql.max_rows, 6)
        with mock.patch.object(Path, "home", return_value=self.folder / "home"):
            self.assertEqual(load_settings(self.project, env={}).user_dir,
                             self.folder / "home" / ".pcldb")


class TestSecrets(ConfigCase):
    SECRETS = ("pw-owner-11", "pw-app-22", "pw-reader-33", "pcl_token_44", "sk-env-key-55")

    def settings_with_secrets(self):
        return self.load({
            "PCLDB_PG_OWNER_DSN": "host=h user=o password=pw-owner-11",
            "PCLDB_PG_APP_DSN": "postgresql://pcl_app:pw-app-22@h/db",
            "PCLDB_PG_READER_DSN": "host=h user=pcl_reader password=pw-reader-33",
            "PCLDB_TOKEN": "pcl_token_44", "PCLDB_LLM_API_KEY": "sk-env-key-55"})

    def test_repr_and_str_of_every_settings_object_show_no_secret(self):
        settings = self.settings_with_secrets()
        objects = [settings, settings.database, settings.database.postgresql,
                   settings.database.postgresql.external, settings.llm, settings.server]
        for item in objects:
            for text in (repr(item), str(item), "%s" % (item,), "{}".format(item)):
                for secret in self.SECRETS:
                    self.assertNotIn(secret, text)
        self.assertIn("<hidden>", repr(settings))
        shown = json.dumps(settings.to_dict())
        for secret in self.SECRETS:
            self.assertNotIn(secret, shown)
        # The values themselves are there for the code that needs them.
        self.assertIn("pw-owner-11", settings.database.postgresql.external.owner)
        self.assertEqual(settings.token, "pcl_token_44")
        self.assertEqual(settings.llm_api_key(), "sk-env-key-55")

    def test_changed_settings_keep_the_secrets_and_stay_silent(self):
        changed = self.settings_with_secrets().changed({"sql.max_rows": 5})
        self.assertEqual(changed.sql.max_rows, 5)
        self.assertEqual(changed.token, "pcl_token_44")
        self.assertEqual(changed.llm_api_key(), "sk-env-key-55")
        self.assertIn("pw-app-22", changed.database.postgresql.external.app)
        for secret in self.SECRETS:
            self.assertNotIn(secret, repr(changed))

    def test_llm_api_key_precedence(self):
        self.assertIsNone(self.load().llm_api_key())                    # nothing anywhere

        self.user.mkdir(parents=True)
        (self.user / "arc_llm_api_key").write_text("sk-user-file\n", encoding="utf-8")
        self.assertEqual(self.load().llm_api_key(), "sk-user-file")     # U/arc_llm_api_key

        (self.project / "keys").mkdir()
        (self.project / "keys" / "llm.txt").write_bytes(b"\xef\xbb\xbf  sk-configured  \r\nrest\n")
        self.write({"llm": {"api_key_file": "keys/llm.txt"}})           # relative to the project
        self.assertEqual(self.load().llm_api_key(), "sk-configured")
        self.write({"llm": {"api_key_file": str(self.project / "keys" / "llm.txt")}})
        self.assertEqual(self.load().llm_api_key(), "sk-configured")

        self.assertEqual(self.load({"PCLDB_LLM_API_KEY": "sk-env"}).llm_api_key(), "sk-env")

        self.write({"llm": {"api_key_file": "keys/missing.txt"}})       # falls through
        self.assertEqual(self.load().llm_api_key(), "sk-user-file")
        (self.user / "arc_llm_api_key").write_text("\n", encoding="utf-8")
        self.assertIsNone(self.load().llm_api_key())                    # an empty file is no key


class TestForTest(ConfigCase):
    def test_it_is_isolated_in_the_temporary_folder(self):
        with mock.patch.dict(os.environ, {"PCLDB_ENGINE": "postgresql", "PCLDB_DIR": "/nowhere",
                                          "PCLDB_LLM_API_KEY": "sk-real", "PCLDB_TOKEN": "t",
                                          "PCLDB_PG_APP_DSN": "host=elsewhere"}):
            settings = Settings.for_test(self.project)
        self.assertEqual(settings.project_dir, self.project)
        self.assertEqual(settings.user_dir, self.project / "user")
        self.assertEqual(settings.var_dir, self.project / "var")
        self.assertEqual(settings.database.engine, "sqlite")
        self.assertEqual(settings.database.postgresql.mode, "local")
        self.assertIsNone(settings.database.postgresql.external.app)
        self.assertIsNone(settings.token)
        self.assertIsNone(settings.llm_api_key())
        self.assertFalse((self.project / "var").exists())      # nothing is created by settings
        for path in (settings.sqlite_dir, settings.pgdata_dir, settings.local_secrets_path,
                     settings.log_dir, settings.config_path, settings.llm_api_key_path,
                     settings.globus_tokens_path):
            self.assertIn(self.project, path.parents)

    def test_either_engine_and_safe_ports(self):
        for engine in ("sqlite", "postgresql"):
            settings = Settings.for_test(self.project, engine=engine)
            self.assertEqual(settings.database.engine, engine)
            ports = {settings.server.port, settings.database.postgresql.port}
            self.assertEqual(len(ports), 2)
            self.assertFalse(ports & {8787, 15439})
        with self.assertRaises(PclError):
            Settings.for_test(self.project, engine="oracle")

    def test_the_llm_address_cannot_reach_the_network(self):
        self.assertTrue(Settings.for_test(self.project).llm.base_url.startswith("http://127.0.0.1:"))

    def test_changes(self):
        settings = Settings.for_test(self.project, engine="postgresql", changes={
            "sql.max_rows": 5, "search.visible_to": ["urn:x"], "llm.enabled": False,
            "database.postgresql.port": 23456, "database.sqlite.folder": "elsewhere/db"})
        self.assertEqual(settings.sql.max_rows, 5)
        self.assertEqual(settings.search.visible_to, ("urn:x",))
        self.assertFalse(settings.llm.enabled)
        self.assertEqual(settings.database.postgresql.port, 23456)
        self.assertEqual(settings.sqlite_dir, self.project / "elsewhere" / "db")
        with self.assertRaises(PclError) as caught:
            Settings.for_test(self.project, changes={"sql.maxrows": 5})
        self.assertIn("sql.maxrows", str(caught.exception))
        with self.assertRaises(PclError):
            Settings.for_test(self.project, changes={"sql.max_rows": "many"})

    def test_settings_are_frozen_and_changed_makes_a_copy(self):
        settings = Settings.for_test(self.project)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.server_url = "x"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.sql.max_rows = 5
        changed = settings.changed({"sql.max_rows": 5, "database.engine": "postgresql"})
        self.assertEqual((settings.sql.max_rows, changed.sql.max_rows), (1000, 5))
        self.assertEqual((settings.database.engine, changed.database.engine),
                         ("sqlite", "postgresql"))
        self.assertEqual(changed.project_dir, settings.project_dir)
        self.assertEqual(changed.server.port, settings.server.port)
        self.assertEqual(settings.changed({}), settings)
        hash(settings)      # usable as a dictionary key

    def test_an_absolute_sqlite_folder_is_kept(self):
        absolute = self.folder / "db files"
        settings = Settings.for_test(self.project, changes={"database.sqlite.folder": str(absolute)})
        self.assertEqual(settings.sqlite_dir, absolute)


if __name__ == "__main__":
    unittest.main()
