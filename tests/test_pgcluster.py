"""pgcluster.py: finding PostgreSQL's programs (fake folder trees for the three systems) and the
project-local cluster (a real one, in a temporary folder with a space in its path)."""

from __future__ import annotations

import contextlib
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcldb import util
from pcldb.config import Settings
from pcldb.errors import PclError
from pcldb.store import pgcluster
from pcldb.store.pgcluster import LocalCluster, PgBin, find_pg_bins
from tests import support


def make_bin(folder, version=None, suffix="", programs=pgcluster.PROGRAMS):
    """A fake program folder: empty files, and the version in a file beside them."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in programs:
        (folder / (name + suffix)).write_bytes(b"")
    if version is not None:
        (folder / "VERSION").write_text(version, encoding="utf-8")
    return folder


def fake_version(program):
    """What `postgres --version` would say for a fake folder."""
    try:
        text = (Path(program).parent / "VERSION").read_text(encoding="utf-8")
    except OSError:
        return None
    return pgcluster.parse_version("postgres (PostgreSQL) " + text)


class DiscoveryCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pcldb bins "))
        self.addCleanup(support.remove_tree, self.root)

    def find(self, system, env=None, explicit=None):
        return find_pg_bins(explicit, env=env or {}, system=system, root=self.root,
                            version_of=fake_version)

    def versions(self, found):
        return [item.version for item in found]


class TestDiscoveryMac(DiscoveryCase):
    def setUp(self):
        super().setUp()
        root = self.root
        self.brew17 = make_bin(root / "opt/homebrew/opt/postgresql@17/bin", "17.12 (Homebrew)")
        self.brew18 = make_bin(root / "opt/homebrew/opt/postgresql@18/bin", "18.7 (Homebrew)")
        self.intel16 = make_bin(root / "usr/local/opt/postgresql@16/bin", "16.4")
        self.app15 = make_bin(root / "Applications/Postgres.app/Contents/Versions/15/bin", "15.9")
        self.edb19 = make_bin(root / "Library/PostgreSQL/19/bin", "19.0")
        self.on_path = make_bin(root / "somewhere/bin", "17.4")
        # Folders that must not count: client programs only (as Anaconda and libpq have them),
        # Windows names on a Mac, and a folder whose postgres does not answer.
        self.clients = make_bin(root / "opt/anaconda3/bin", "17.4", programs=("pg_config", "psql"))
        make_bin(root / "opt/homebrew/opt/postgresql@14/bin", "14.25", programs=("initdb", "pg_ctl"))
        make_bin(root / "opt/homebrew/opt/postgresql@20/bin", "20.0", suffix=".exe")
        make_bin(root / "Library/PostgreSQL/21/bin", None)
        self.env = {"PATH": os.pathsep.join([str(root / "nowhere at all"), str(self.clients),
                                             str(self.on_path)])}

    def test_the_usual_places_and_path_newest_first(self):
        found = self.find("darwin", self.env)
        self.assertEqual(self.versions(found), ["19.0", "18.7", "17.12", "17.4", "16.4"])
        self.assertEqual([item.bin_dir for item in found],
                         [self.edb19, self.brew18, self.brew17, self.on_path, self.intel16])
        self.assertEqual([(item.major, item.minor) for item in found],
                         [(19, 0), (18, 7), (17, 12), (17, 4), (16, 4)])
        for item in found:
            self.assertIsInstance(item, PgBin)
            self.assertEqual(item.suffix, "")
            self.assertEqual(item.program("pg_ctl"), item.bin_dir / "pg_ctl")

    def test_too_old_versions_are_left_out_but_known(self):
        usable, too_old = pgcluster.scan_pg_bins(None, env=self.env, system="darwin",
                                                 root=self.root, version_of=fake_version)
        self.assertNotIn("15.9", self.versions(usable))
        self.assertEqual([(item.version, item.bin_dir) for item in too_old],
                         [("15.9", self.app15)])
        self.assertEqual(pgcluster.MIN_MAJOR, 16)

    def test_an_explicit_folder_comes_first(self):
        found = self.find("darwin", self.env, explicit=str(self.brew17))
        self.assertEqual(self.versions(found), ["17.12", "19.0", "18.7", "17.4", "16.4"])
        self.assertEqual(found[0].bin_dir, self.brew17)

    def test_the_environment_variable_names_the_explicit_folder(self):
        env = dict(self.env, PCLDB_PG_BIN=str(self.intel16))
        self.assertEqual(self.versions(self.find("darwin", env)),
                         ["16.4", "19.0", "18.7", "17.12", "17.4"])
        # An explicit argument wins over the variable.
        found = self.find("darwin", env, explicit=str(self.on_path))
        self.assertEqual(found[0].version, "17.4")

    def test_an_explicit_folder_without_the_programs_is_not_in_the_list(self):
        found = self.find("darwin", self.env, explicit=str(self.clients))
        self.assertEqual(self.versions(found), ["19.0", "18.7", "17.12", "17.4", "16.4"])
        found = self.find("darwin", self.env, explicit=str(self.root / "missing"))
        self.assertEqual(len(found), 5)

    def test_a_folder_reached_twice_counts_once(self):
        env = {"PATH": os.pathsep.join([str(self.brew18), str(self.brew18) + os.sep,
                                        str(self.on_path)])}
        found = self.find("darwin", env, explicit=str(self.brew18))
        self.assertEqual(self.versions(found), ["18.7", "19.0", "17.12", "17.4", "16.4"])

    @unittest.skipIf(os.name == "nt", "links need special rights on Windows")
    def test_a_link_to_a_version_counts_once(self):
        versions = self.root / "Applications/Postgres.app/Contents/Versions"
        make_bin(versions / "18/bin", "18.3")
        os.symlink(versions / "18", versions / "latest")
        found = self.find("darwin", {})
        self.assertEqual(self.versions(found).count("18.3"), 1)

    def test_other_systems_do_not_look_in_the_mac_places(self):
        self.assertEqual(self.find("linux", {}), [])
        self.assertEqual(self.find("windows", {}), [])


class TestDiscoveryWindows(DiscoveryCase):
    def setUp(self):
        super().setUp()
        root = self.root
        self.program_files = root / "Program Files"
        self.edb17 = make_bin(self.program_files / "PostgreSQL/17/bin", "17.12", ".exe")
        self.edb16 = make_bin(self.program_files / "PostgreSQL/16/bin", "16.16", ".exe")
        self.user18 = make_bin(root / "Users/demo/AppData/Local/Programs/PostgreSQL/18/bin",
                               "18.7", ".exe")
        self.pgbin = make_bin(root / "ci/PostgreSQL/17/bin", "17.5", ".exe")
        self.pgroot = make_bin(root / "ci/root/bin", "16.1", ".exe")
        self.scoop = make_bin(root / "Users/demo/scoop/apps/postgresql/current/bin", "19.0",
                              ".exe")
        self.conda = make_bin(root / "Users/demo/miniconda3/envs/lab/Library/bin", "18.1", ".exe")
        self.on_path = make_bin(root / "tools/pg/bin", "17.9", ".exe")
        make_bin(self.program_files / "PostgreSQL/15/bin", "15.20", ".exe")           # too old
        make_bin(self.program_files / "PostgreSQL/20/bin", "20.0")                    # no .exe
        self.env = {
            "ProgramFiles": str(self.program_files),
            "LOCALAPPDATA": str(root / "Users/demo/AppData/Local"),
            "PGBIN": str(self.pgbin),
            "PGROOT": str(root / "ci/root"),
            "USERPROFILE": str(root / "Users/demo"),
            "CONDA_PREFIX": str(root / "Users/demo/miniconda3/envs/lab"),
            "PATH": os.pathsep.join(['"%s"' % self.on_path, str(root / "Windows/System32"), ""]),
        }

    def test_every_windows_place_with_exe_names_newest_first(self):
        found = self.find("windows", self.env)
        self.assertEqual(self.versions(found),
                         ["19.0", "18.7", "18.1", "17.12", "17.9", "17.5", "16.16", "16.1"])
        self.assertEqual([item.bin_dir for item in found],
                         [self.scoop, self.user18, self.conda, self.edb17, self.on_path,
                          self.pgbin, self.edb16, self.pgroot])
        for item in found:
            self.assertEqual(item.suffix, ".exe")
            self.assertEqual(item.program("initdb").name, "initdb.exe")
            self.assertTrue(item.program("postgres").is_file())

    def test_each_place_is_found_on_its_own(self):
        for key, expected in (("ProgramFiles", ["17.12", "16.16"]), ("LOCALAPPDATA", ["18.7"]),
                              ("PGBIN", ["17.5"]), ("PGROOT", ["16.1"]),
                              ("USERPROFILE", ["19.0"]), ("CONDA_PREFIX", ["18.1"]),
                              ("PATH", ["17.9"])):
            self.assertEqual(self.versions(self.find("windows", {key: self.env[key]})), expected,
                             key)

    def test_names_without_exe_do_not_count_on_windows_and_exe_names_not_elsewhere(self):
        only_plain = {"ProgramFiles": str(self.program_files)}
        self.assertNotIn("20.0", self.versions(self.find("windows", only_plain)))
        self.assertEqual(self.find("linux", {"PATH": str(self.edb17)}), [])
        self.assertEqual(self.find("darwin", {"PATH": str(self.edb17)}), [])

    def test_an_explicit_folder_comes_first(self):
        found = self.find("windows", self.env, explicit=str(self.edb16))
        self.assertEqual(found[0].bin_dir, self.edb16)
        self.assertEqual(self.versions(found)[1:],
                         ["19.0", "18.7", "18.1", "17.12", "17.9", "17.5", "16.1"])


class TestDiscoveryLinux(DiscoveryCase):
    def test_debian_and_red_hat_places_and_path(self):
        debian16 = make_bin(self.root / "usr/lib/postgresql/16/bin", "16.16 (Ubuntu 16.16-1)")
        debian18 = make_bin(self.root / "usr/lib/postgresql/18/bin", "18.7 (Ubuntu 18.7-1)")
        redhat17 = make_bin(self.root / "usr/pgsql-17/bin", "17.12")
        local = make_bin(self.root / "home/demo/pg/bin", "19devel")
        make_bin(self.root / "usr/lib/postgresql/14/bin", "14.25")
        make_bin(self.root / "usr/bin", None, programs=("psql", "pg_config"))
        env = {"PATH": os.pathsep.join([str(self.root / "usr/bin"), str(local)])}
        found = self.find("linux", env)
        self.assertEqual([item.bin_dir for item in found], [local, debian18, redhat17, debian16])
        self.assertEqual(self.versions(found), ["19.0", "18.7", "17.12", "16.16"])
        self.assertEqual(self.find("darwin", {}), [])


class TestVersionsAndEnvironment(unittest.TestCase):
    def test_parse_version(self):
        for text, expected in (
                ("postgres (PostgreSQL) 18.7 (Homebrew)\n", (18, 7, "18.7")),
                ("postgres (PostgreSQL) 17.12", (17, 12, "17.12")),
                ("postgres (PostgreSQL) 16.16 (Ubuntu 16.16-1.pgdg24.04+1)", (16, 16, "16.16")),
                ("postgres (PostgreSQL) 19devel", (19, 0, "19.0")),
                ("postgres (PostgreSQL) 18beta2", (18, 0, "18.0")),
                ("psql (PostgreSQL) 9.6.24", (9, 6, "9.6")),
                ("command not found", None), ("", None), (None, None)):
            self.assertEqual(pgcluster.parse_version(text), expected, text)

    def test_read_version_of_something_that_cannot_run(self):
        self.assertIsNone(pgcluster.read_version(Path(tempfile.gettempdir()) / "no such program"))

    def test_the_child_environment_has_no_pg_variables(self):
        env = {"PATH": "/bin", "HOME": "/home/demo", "PGDATA": "/elsewhere", "PGPORT": "5432",
               "PGHOST": "db", "PGUSER": "postgres", "PGPASSWORD": "secret", "PGDATABASE": "x",
               "PGSERVICE": "s", "PGOPTIONS": "-c x=y", "PGPASSFILE": "/p",
               "PGCLIENTENCODING": "LATIN1", "LANG": "de_DE.UTF-8", "PCLDB_TOKEN": "t",
               # What the GitHub Windows image (and the EDB installer) sets, and a name in mixed
               # capitals (names on Windows ignore case).
               "PGBIN": "C:\\Program Files\\PostgreSQL\\17\\bin",
               "PGROOT": "C:\\Program Files\\PostgreSQL\\17", "PgSslRootCert": "x",
               "pgappname": "y", "PYTHONPATH": "kept", "APGAR": "kept too"}
        for system in ("darwin", "linux", "windows"):
            child = pgcluster.child_environment(env, system)
            self.assertEqual(sorted(name for name in child if name.upper().startswith("PG")), [],
                             system)
            self.assertEqual(child["PATH"], "/bin")
            self.assertEqual(child["LANG"], "de_DE.UTF-8")
            self.assertEqual((child["PYTHONPATH"], child["APGAR"]), ("kept", "kept too"))
            self.assertNotIn("LC_ALL", child)
        self.assertIn("PGPASSWORD", env)        # the caller's own dict is not changed
        self.assertIn("PGBIN", env)

    def test_on_a_mac_without_a_locale_lc_all_is_set(self):
        for env in ({}, {"LANG": "", "LC_ALL": ""}, {"LANG": "  "}, {"LC_CTYPE": "UTF-8"}):
            self.assertEqual(pgcluster.child_environment(env, "darwin").get("LC_ALL"),
                             "en_US.UTF-8", env)
            self.assertNotEqual(pgcluster.child_environment(env, "linux").get("LC_ALL"),
                                "en_US.UTF-8")
            self.assertNotEqual(pgcluster.child_environment(env, "windows").get("LC_ALL"),
                                "en_US.UTF-8")
        self.assertEqual(pgcluster.child_environment({"LC_ALL": "C"}, "darwin")["LC_ALL"], "C")
        self.assertNotIn("LC_ALL", pgcluster.child_environment({"LANG": "en_GB.UTF-8"}, "darwin"))

    def test_the_real_environment_is_the_default(self):
        with mock.patch.dict(os.environ, {"PGPORT": "1", "PCLDB_TEST_MARK": "here"}):
            child = pgcluster.child_environment()
        self.assertNotIn("PGPORT", child)
        self.assertEqual(child["PCLDB_TEST_MARK"], "here")
        self.assertIn(pgcluster.current_system(), ("darwin", "linux", "windows"))


class TestChoosing(unittest.TestCase):
    def setUp(self):
        self.project = support.TempProject("postgresql")
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings
        self.good = PgBin(Path("/opt/pg/18/bin"), "18.7", 18, 7)
        self.old = PgBin(Path("/opt/pg/14/bin"), "14.25", 14, 25)
        patcher = mock.patch.dict(os.environ)       # these tests decide the variable themselves
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PCLDB_PG_BIN", None)

    def choose(self, settings, usable, too_old=()):
        with mock.patch.object(pgcluster, "scan_pg_bins",
                               return_value=(list(usable), list(too_old))) as scan:
            try:
                return pgcluster.choose_pg_bin(settings), scan
            except PclError as error:
                return error, scan

    def test_the_first_usable_folder_is_taken(self):
        chosen, scan = self.choose(self.settings, [self.good, self.old])
        self.assertIs(chosen, self.good)
        scan.assert_called_once_with(None)

    def test_nothing_found_says_how_to_install_or_switch(self):
        error, _ = self.choose(self.settings, [])
        self.assertIsInstance(error, PclError)
        self.assertEqual(str(error), (
            "PostgreSQL's programs were not found. Install PostgreSQL (Mac: brew install "
            "postgresql@18; Windows: winget install PostgreSQL.PostgreSQL.18) or run: "
            "pcldb setup --engine sqlite"))
        self.assertEqual((error.code, error.status), ("postgres_missing", 503))

    def test_a_project_that_was_set_up_is_not_told_to_switch_engines(self):
        # NEW-07: "pcldb setup --engine sqlite" is always refused on a project that exists.
        self.settings.config_path.write_text('{"database": {"engine": "postgresql"}}',
                                             encoding="utf-8")
        self.addCleanup(self.settings.config_path.unlink)
        for too_old in ((), (self.old,)):
            error, _ = self.choose(self.settings, [], too_old)
            self.assertIsInstance(error, PclError)
            self.assertNotIn("setup --engine sqlite", str(error))
            self.assertIn("database.postgresql.bin_dir", str(error))
            self.assertIn("brew install postgresql@18", str(error))

    def test_only_an_old_version_says_which_and_what_is_needed(self):
        error, _ = self.choose(self.settings, [], [self.old])
        self.assertIsInstance(error, PclError)
        self.assertIn("14.25", str(error))
        self.assertIn("16", str(error))
        self.assertIn("pcldb setup --engine sqlite", str(error))

    def test_a_configured_folder_must_be_the_one_that_is_used(self):
        folder = self.project.path / "my pg" / "bin"
        settings = self.settings.changed({"database.postgresql.bin_dir": str(folder)})
        mine = PgBin(folder, "17.12", 17, 12)
        chosen, scan = self.choose(settings, [mine, self.good])
        self.assertIs(chosen, mine)
        scan.assert_called_once_with(str(folder))
        # The folder has no programs: do not silently take another version.
        error, _ = self.choose(settings, [self.good])
        self.assertIsInstance(error, PclError)
        for part in (str(folder), "database.postgresql.bin_dir", "PCLDB_PG_BIN"):
            self.assertIn(part, str(error))
        error, _ = self.choose(settings, [self.good], [PgBin(folder, "15.2", 15, 2)])
        self.assertIn("15.2", str(error))
        self.assertIn("16", str(error))

    def test_the_environment_variable_is_just_as_binding(self):
        folder = self.project.path / "env pg" / "bin"
        os.environ["PCLDB_PG_BIN"] = str(folder)
        mine = PgBin(folder, "17.12", 17, 12)
        chosen, scan = self.choose(self.settings, [mine, self.good])
        self.assertIs(chosen, mine)
        scan.assert_called_once_with(str(folder))
        error, _ = self.choose(self.settings, [self.good])      # never another version instead
        self.assertIsInstance(error, PclError)
        self.assertIn("PCLDB_PG_BIN", str(error))
        # The setting wins over the variable.
        other = self.project.path / "set pg" / "bin"
        settings = self.settings.changed({"database.postgresql.bin_dir": str(other)})
        _, scan = self.choose(settings, [PgBin(other, "18.7", 18, 7)])
        scan.assert_called_once_with(str(other))


class TestSecrets(unittest.TestCase):
    def setUp(self):
        self.project = support.TempProject("postgresql")
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings

    def test_made_once_private_and_random(self):
        with self.assertRaises(PclError) as caught:
            pgcluster.read_local_secrets(self.settings)
        self.assertEqual(caught.exception.code, "db_secrets_missing")
        self.assertIn("pcldb setup", str(caught.exception))
        first = pgcluster.ensure_local_secrets(self.settings)
        self.assertEqual(sorted(first), ["app", "owner", "reader"])
        self.assertEqual(len(set(first.values())), 3)
        for value in first.values():
            self.assertRegex(value, r"^[A-Za-z0-9_-]{32,}$")
        self.assertEqual(pgcluster.ensure_local_secrets(self.settings), first)
        self.assertEqual(pgcluster.read_local_secrets(self.settings), first)
        path = self.settings.local_secrets_path
        self.assertEqual(path, self.project.path / "var" / "local_secrets.json")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        other = support.TempProject("postgresql")
        self.addCleanup(other.cleanup)
        self.assertNotEqual(pgcluster.ensure_local_secrets(other.settings), first)

    def test_a_damaged_file_is_a_clear_error_without_its_content(self):
        path = self.settings.local_secrets_path
        path.parent.mkdir(parents=True)
        for content in ('{"owner": "only-one-secret"}', '["list"]', "{broken",
                        '{"owner": "a", "app": "", "reader": "c"}'):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(PclError, msg=content) as caught:
                pgcluster.read_local_secrets(self.settings)
            self.assertNotIn("only-one-secret", str(caught.exception))
            self.assertIn("local_secrets.json", str(caught.exception))


def stale_pid_file(cluster, pid):
    """A postmaster.pid as a crashed server leaves it behind."""
    lines = [str(pid), str(cluster.data_dir), "1795215968", str(cluster.port), "", "127.0.0.1",
             "", "ready   "]
    (cluster.data_dir / "postmaster.pid").write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestLocalCluster(unittest.TestCase):
    """One real cluster for the class. Every test leaves it stopped."""

    @classmethod
    def setUpClass(cls):
        support.require_postgres()
        cls.project = support.TempProject("postgresql", prefix="pcldb cluster with space ")
        cls.addClassCleanup(cls.project.cleanup)
        cls.settings = cls.project.settings
        cls.cluster = LocalCluster(cls.settings, test_mode=True)
        cls.addClassCleanup(cls.cluster.destroy)
        cls.created = cls.cluster.init()
        cls.passwords = pgcluster.read_local_secrets(cls.settings)

    def setUp(self):
        self.addCleanup(self.cluster.stop)

    def owner(self, cluster=None, password=None, **more):
        import psycopg
        cluster = cluster or self.cluster
        connection = psycopg.connect(
            host="127.0.0.1", port=cluster.port, dbname="postgres", user="pcl_owner",
            password=password or self.passwords["owner"], connect_timeout=5, autocommit=True,
            **more)
        self.addCleanup(connection.close)
        return connection

    def test_init_made_a_cluster_in_a_folder_with_a_space(self):
        self.assertTrue(self.created)
        self.assertIn(" ", str(self.cluster.data_dir))
        self.assertEqual(self.cluster.data_dir, self.project.path / "var" / "pgdata")
        self.assertTrue(self.cluster.is_initialized())
        self.assertEqual(self.cluster.data_version(), str(self.cluster.pg_bin.major))
        self.assertFalse(self.cluster.init())               # there already: nothing to do
        # The password file given to initdb is gone, and no password is in what initdb printed.
        left = [path.name for path in self.settings.var_dir.iterdir()]
        self.assertEqual(sorted(left), ["local_secrets.json", "log", "pgdata"])
        printed = (self.settings.log_dir / "initdb.log").read_bytes()
        for value in self.passwords.values():
            self.assertNotIn(value.encode(), printed)
        if self.cluster.pg_bin.major >= 17:
            self.assertEqual(self.cluster.notices, [])

    def test_the_configuration_block(self):
        text = (self.cluster.data_dir / "postgresql.conf").read_text(encoding="utf-8")
        block = text[text.index(pgcluster._CONF_BEGIN):]
        self.assertTrue(block.rstrip().endswith(pgcluster._CONF_END))
        for line in ("listen_addresses = '127.0.0.1'", "port = %d" % self.cluster.port,
                     "unix_socket_directories = ''", "timezone = 'UTC'", "log_timezone = 'UTC'",
                     "logging_collector = on", "log_directory = 'log'"):
            self.assertIn("\n" + line + "\n", block, line)
        self.assertEqual(text.count(pgcluster._CONF_BEGIN), 1)
        self.cluster._write_conf()
        self.cluster._write_conf()
        again = (self.cluster.data_dir / "postgresql.conf").read_text(encoding="utf-8")
        self.assertEqual(again, text)                       # written again: not appended again
        rules = [line.split() for line in (self.cluster.data_dir / "pg_hba.conf").read_text(
            encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
        self.assertTrue(rules)
        for rule in rules:
            self.assertEqual(rule[-1], "scram-sha-256", rule)   # a password everywhere: no trust

    def test_start_status_stop(self):
        before = self.cluster.status()
        self.assertEqual(sorted(before), ["bin_dir", "data_dir", "initialized", "pid", "port",
                                          "running", "version"])
        self.assertEqual((before["initialized"], before["running"], before["pid"]),
                         (True, False, None))
        self.assertTrue(self.cluster.start())
        status = self.cluster.status()
        self.assertEqual((status["initialized"], status["running"]), (True, True))
        self.assertIsInstance(status["pid"], int)
        self.assertEqual(status["port"], self.settings.database.postgresql.port)
        self.assertEqual(status["version"], self.cluster.pg_bin.version)
        self.assertEqual(status["data_dir"], str(self.cluster.data_dir))
        self.assertEqual(status["bin_dir"], str(self.cluster.pg_bin.bin_dir))
        self.assertFalse(self.cluster.start())              # running already
        self.assertFalse(self.cluster.ensure_running())

        connection = self.owner()
        def show(name):
            return connection.execute("SELECT current_setting(%s)", [name]).fetchone()[0]
        self.assertEqual(show("listen_addresses"), "127.0.0.1")
        self.assertEqual(show("unix_socket_directories"), "")
        self.assertEqual(show("TimeZone"), "UTC")
        self.assertEqual(show("log_timezone"), "UTC")
        self.assertEqual(show("server_encoding"), "UTF8")
        self.assertEqual(show("logging_collector"), "on")
        self.assertEqual(show("password_encryption"), "scram-sha-256")
        self.assertEqual(int(show("port")), self.cluster.port)
        self.assertEqual(connection.execute(
            "SELECT rolname, rolsuper FROM pg_roles WHERE oid = 10").fetchone(),
            ("pcl_owner", True))
        collate, ctype = connection.execute(
            "SELECT datcollate, datctype FROM pg_database WHERE datname = 'template0'").fetchone()
        self.assertEqual((collate, ctype), ("C", "C"))
        if self.cluster.pg_bin.major >= 17:
            self.assertEqual(connection.execute(
                "SELECT datlocprovider, datlocale FROM pg_database WHERE datname = 'template0'"
            ).fetchone(), ("b", "C.UTF-8"))
        self.assertTrue(connection.execute("SELECT 'B' < 'a'").fetchone()[0])   # code points
        connection.close()

        import psycopg
        with self.assertRaises(psycopg.OperationalError):       # a wrong password is refused
            self.owner(password="not-the-password")

        self.assertTrue(self.cluster.stop())
        after = self.cluster.status()
        self.assertEqual((after["initialized"], after["running"], after["pid"]),
                         (True, False, None))
        self.assertFalse(self.cluster.stop())               # not running: not an error
        self.assertFalse(self.cluster.stop())
        self.assertFalse((self.cluster.data_dir / "postmaster.pid").exists())

    def test_ensure_running_says_whether_it_started_the_server(self):
        self.assertTrue(self.cluster.ensure_running())
        self.assertFalse(self.cluster.ensure_running())
        self.assertFalse(LocalCluster(self.settings).ensure_running())    # another object: same
        self.assertTrue(LocalCluster(self.settings).status()["running"])

    def test_pg_ctl_start_gets_no_pipe_and_a_clean_environment(self):
        calls = []
        real_run = subprocess.run

        def recording(command, **options):
            calls.append((list(command), dict(options)))
            return real_run(command, **options)

        with mock.patch.dict(os.environ, {"PGPORT": "1", "PGDATA": str(self.project.path),
                                          "PGUSER": "nobody", "PGPASSWORD": "wrong",
                                          "PGHOST": "192.0.2.1", "PGDATABASE": "nothing",
                                          "PGBIN": str(self.project.path), "PGROOT": "x",
                                          "PgMixedCase": "y"}):
            for name in ("LANG", "LC_ALL", "LC_MESSAGES", "LC_CTYPE"):
                os.environ.pop(name, None)      # a caller with no locale at all
            with mock.patch.object(pgcluster.subprocess, "run", recording):
                self.assertTrue(self.cluster.start())
        self.assertTrue(self.cluster.status()["running"])
        starts = [(command, options) for command, options in calls if "start" in command]
        self.assertEqual(len(starts), 1)
        command, options = starts[0]
        self.assertEqual(Path(command[0]).stem, "pg_ctl")
        self.assertIn("-w", command)
        self.assertEqual(command[command.index("-D") + 1], str(self.cluster.data_dir))
        self.assertEqual(command[command.index("-l") + 1],
                         str(self.settings.log_dir / "postgresql.log"))
        self.assertNotIn("-o", command)                     # settings live in postgresql.conf
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertNotIn(options["stdout"], (subprocess.PIPE, None))
        self.assertTrue(hasattr(options["stdout"], "fileno"))   # a real file
        self.assertEqual(Path(options["stdout"].name).parent, self.settings.log_dir)
        self.assertIs(options["stderr"], subprocess.STDOUT)
        self.assertFalse(options.get("capture_output"))
        self.assertFalse(options.get("shell"))
        for command, options in calls:
            self.assertIsInstance(command, list)            # never a shell string
            self.assertFalse(options.get("shell"))
            environment = options["env"]
            self.assertEqual([name for name in environment if name.upper().startswith("PG")],
                             [])
            if sys.platform == "darwin":
                self.assertEqual(environment.get("LC_ALL"), "en_US.UTF-8")
        self.assertTrue((self.settings.log_dir / "pg_ctl.log").stat().st_size > 0)
        self.assertTrue((self.settings.log_dir / "postgresql.log").is_file())

    def test_a_busy_port_is_a_clear_error(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
            if os.name != "nt":
                other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            other.bind(("127.0.0.1", self.cluster.port))
            other.listen(1)
            for action in (self.cluster.start, self.cluster.ensure_running):
                with self.assertRaises(PclError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "port_in_use")
                self.assertIn("Port %d" % self.cluster.port, str(caught.exception))
                self.assertIn("database.postgresql.port", str(caught.exception))
                self.assertIn("pcldb_config.json", str(caught.exception))
            self.assertFalse(self.cluster.status()["running"])
        self.assertTrue(self.cluster.start())               # the port is free again

    def test_a_data_folder_of_another_version_is_a_clear_error(self):
        marker = self.cluster.data_dir / "PG_VERSION"
        original = marker.read_bytes()
        self.addCleanup(marker.write_bytes, original)
        marker.write_text("15\n", encoding="ascii")
        with self.assertRaises(PclError) as caught:
            self.cluster.start()
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, "postgres_version_mismatch")
        self.assertIn("made by PostgreSQL 15", message)
        self.assertIn("PostgreSQL %s" % self.cluster.pg_bin.version, message)
        self.assertIn("database.postgresql.bin_dir", message)
        self.assertIn("PCLDB_PG_BIN", message)
        self.assertFalse(self.cluster.status()["running"])
        marker.write_bytes(original)
        self.assertTrue(self.cluster.start())

    def test_a_left_over_pid_file_of_a_dead_process_does_not_block_the_start(self):
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        stale_pid_file(self.cluster, finished.pid)
        self.assertFalse(self.cluster.status()["running"])
        self.assertTrue(self.cluster.start())
        status = self.cluster.status()
        self.assertTrue(status["running"])
        self.assertNotEqual(status["pid"], finished.pid)

    @unittest.skipIf(os.name == "nt", "on Windows only a PostgreSQL process counts as alive")
    def test_a_left_over_pid_file_whose_pid_another_program_has_does_not_block_the_start(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        stale_pid_file(self.cluster, other.pid)
        self.assertFalse(self.cluster.status()["running"])  # pg_ctl alone would say "running"
        self.assertTrue(self.cluster.start())
        status = self.cluster.status()
        self.assertTrue(status["running"])
        self.assertNotEqual(status["pid"], other.pid)
        self.assertIsNone(other.poll())                     # the other program was not touched

    def test_a_pid_file_is_never_removed_while_a_server_answers(self):
        self.assertTrue(self.cluster.start())
        self.assertFalse(self.cluster._remove_left_over_pid_file())
        self.assertTrue((self.cluster.data_dir / "postmaster.pid").is_file())
        self.assertTrue(self.cluster.status()["running"])

    def test_a_start_that_fails_shows_what_the_server_said(self):
        path = self.cluster.data_dir / "postgresql.conf"
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        path.write_bytes(original + b"shared_buffers = 'nonsense'\n")
        with self.assertRaises(PclError) as caught:
            self.cluster.start()
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, "postgres_failed")
        self.assertIn("did not start", message)
        self.assertIn(str(self.settings.log_dir / "postgresql.log"), message)
        self.assertIn("shared_buffers", message)
        self.assertLessEqual(len(message.splitlines()), 45)     # the last 20 lines, not the file
        self.assertFalse(self.cluster.status()["running"])
        path.write_bytes(original)
        self.assertTrue(self.cluster.start())

    def test_a_changed_port_setting_reaches_the_server(self):
        other_port = util.free_port()
        moved = LocalCluster(self.settings.changed({"database.postgresql.port": other_port}),
                             test_mode=True)
        self.addCleanup(moved.stop)
        self.assertTrue(moved.start())
        self.assertEqual(moved.status()["port"], other_port)
        connection = self.owner(moved)
        self.assertEqual(int(connection.execute("SELECT current_setting('port')").fetchone()[0]),
                         other_port)
        connection.close()
        # The first object sees the same server; it runs on the port that server was given.
        self.assertTrue(self.cluster.status()["running"])
        self.assertEqual(self.cluster.status()["port"], other_port)
        self.assertTrue(moved.stop())
        self.assertTrue(self.cluster.start())
        self.assertEqual(self.cluster.status()["port"], self.cluster.port)

    def test_without_the_programs_status_still_answers_and_start_says_what_is_missing(self):
        empty = self.project.path / "no programs here"
        empty.mkdir(exist_ok=True)
        lost = LocalCluster(self.settings.changed({"database.postgresql.bin_dir": str(empty)}))
        status = lost.status()
        self.assertEqual((status["initialized"], status["running"], status["bin_dir"]),
                         (True, False, None))
        self.assertEqual(status["version"], self.cluster.data_version())
        for action in (lost.start, lost.ensure_running):
            with self.assertRaises(PclError) as caught:
                action()
            self.assertEqual(caught.exception.code, "postgres_missing")
            self.assertIn("database.postgresql.bin_dir", str(caught.exception))
        # A running server is still seen (by its port) when the programs are gone.
        self.assertTrue(self.cluster.start())
        self.assertTrue(lost.status()["running"])


class TestClusterEdges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.require_postgres()

    def setUp(self):
        self.project = support.TempProject("postgresql")
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings
        self.cluster = LocalCluster(self.settings, test_mode=True)
        self.addCleanup(self.cluster.destroy)

    def test_before_init_nothing_runs_and_start_says_what_to_do(self):
        status = self.cluster.status()
        self.assertEqual((status["initialized"], status["running"], status["pid"]),
                         (False, False, None))
        self.assertEqual(status["port"], self.settings.database.postgresql.port)
        self.assertFalse(self.cluster.stop())
        for action in (self.cluster.start, self.cluster.ensure_running):
            with self.assertRaises(PclError) as caught:
                action()
            self.assertEqual(caught.exception.code, "not_initialized")
            self.assertIn("pcldb setup", str(caught.exception))
        self.assertFalse(self.settings.var_dir.exists())    # asking created nothing

    def test_init_refuses_a_folder_that_holds_something_else(self):
        self.settings.pgdata_dir.mkdir(parents=True)
        (self.settings.pgdata_dir / "notes.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(PclError) as caught:
            self.cluster.init()
        self.assertIn(str(self.settings.pgdata_dir), str(caught.exception))
        self.assertEqual((self.settings.pgdata_dir / "notes.txt").read_text(encoding="utf-8"),
                         "mine")
        self.assertFalse(self.cluster.is_initialized())

    def test_init_into_an_existing_empty_folder_and_destroy(self):
        self.settings.pgdata_dir.mkdir(parents=True)
        (self.settings.pgdata_dir / ".DS_Store").write_bytes(b"what the Finder leaves behind")
        self.assertTrue(self.cluster.init())
        self.assertFalse((self.settings.pgdata_dir / ".DS_Store").exists())
        # pcldb does not make the server's log file itself (on Windows the server, which runs
        # with fewer rights than an administrator's prompt, could then not write to it).
        self.assertFalse(self.cluster.log_file.exists())
        self.assertTrue(self.cluster.start())
        self.cluster.destroy()
        self.assertFalse(self.settings.pgdata_dir.exists())
        self.assertFalse(util.port_answers(self.cluster.port))
        self.assertTrue(self.settings.local_secrets_path.is_file())     # kept for the next init
        self.assertEqual(self.cluster.status()["initialized"], False)
        self.cluster.destroy()                              # nothing there: still fine

    def test_initdb_that_fails_leaves_nothing_and_shows_its_output(self):
        seen = {}

        def broken_initdb(command, **options):
            # What a failing initdb does: it starts on the folder, complains, and gives up.
            password_file = [part for part in command if part.startswith("--pwfile=")][0][9:]
            seen["password"] = Path(password_file).read_text(encoding="utf-8")
            seen["mode"] = stat.S_IMODE(Path(password_file).stat().st_mode)
            self.settings.pgdata_dir.mkdir()
            (self.settings.pgdata_dir / "half made").write_text("x", encoding="utf-8")
            options["stdout"].write(b"initdb: error: this is a broken program\n")
            return subprocess.CompletedProcess(command, 3)

        cluster = LocalCluster(self.settings, test_mode=True)
        cluster._bin = PgBin(self.project.path / "broken bin", "18.0", 18, 0)
        with mock.patch.object(pgcluster.subprocess, "run", broken_initdb):
            with self.assertRaises(PclError) as caught:
                cluster.init()
        self.assertIn("this is a broken program", str(caught.exception))
        self.assertIn(str(self.settings.log_dir / "initdb.log"), str(caught.exception))
        self.assertEqual(caught.exception.code, "postgres_failed")
        self.assertFalse(self.settings.pgdata_dir.exists())             # nothing half made stays
        # initdb got the owner's password in a private file, and that file is gone again.
        passwords = pgcluster.read_local_secrets(self.settings)
        self.assertEqual(seen["password"], passwords["owner"] + "\n")
        if os.name != "nt":
            self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(sorted(path.name for path in self.settings.var_dir.iterdir()),
                         ["local_secrets.json", "log"])
        self.assertNotIn(passwords["owner"], str(caught.exception))

    def test_postgresql_16_gets_plain_c_and_says_so(self):
        """PostgreSQL 16 has no built-in locale provider: other flags, and a sentence."""
        commands = []

        def fake_run(command, **_options):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 1)

        for major, version in ((16, "16.16"), (17, "17.12"), (19, "19.0")):
            cluster = LocalCluster(self.settings, test_mode=False)
            cluster._bin = PgBin(self.project.path / "bin", version, major, 0)
            with mock.patch.object(pgcluster.subprocess, "run", fake_run):
                with self.assertRaises(PclError):
                    cluster.init()              # the stand-in "fails"; only the command matters
            command = commands[-1]
            self.assertEqual(Path(command[0]).name, "initdb")
            for flag in ("-U", "pcl_owner", "--auth=scram-sha-256", "--encoding=UTF8",
                         "--locale=C", "--no-instructions"):
                self.assertIn(flag, command)
            self.assertEqual(len([part for part in command if part.startswith("--pwfile=")]), 1)
            self.assertNotIn("--no-sync", command)          # only test clusters skip the sync
            builtin = ["--locale-provider=builtin", "--builtin-locale=C.UTF-8"]
            if major >= 17:
                self.assertTrue(all(flag in command for flag in builtin), command)
                self.assertEqual(cluster.notices, [])
            else:
                self.assertFalse(any(flag in command for flag in builtin), command)
                self.assertEqual(len(cluster.notices), 1)
                self.assertIn("16.16", cluster.notices[0])
                self.assertIn("plain C", cluster.notices[0])


class TestWindowsLaunch(unittest.TestCase):
    """What cannot run here: how pg_ctl is launched on Windows."""

    def test_pg_ctl_start_gets_its_own_console_and_more_time(self):
        project = support.TempProject("postgresql")
        self.addCleanup(project.cleanup)
        settings = project.settings
        settings.pgdata_dir.mkdir(parents=True)
        (settings.pgdata_dir / "PG_VERSION").write_text("18\n", encoding="ascii")
        (settings.pgdata_dir / "postgresql.conf").write_text("# stand-in\n", encoding="utf-8")
        cluster = LocalCluster(settings)
        cluster._bin = PgBin(project.path / "bin", "18.7", 18, 7, ".exe")
        calls = []

        def fake_run(command, **options):
            calls.append((list(command), dict(options)))
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(pgcluster, "current_system", return_value="windows"), \
                mock.patch.object(pgcluster.subprocess, "run", fake_run), \
                mock.patch.object(pgcluster.subprocess, "CREATE_NO_WINDOW", 0x08000000,
                                  create=True), \
                mock.patch.object(pgcluster.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200,
                                  create=True):
            self.assertTrue(cluster.start())
        (command, options), = [call for call in calls if "start" in call[0]]
        self.assertEqual(Path(command[0]).name, "pg_ctl.exe")
        # Ctrl+C in the window that runs `pcldb serve` must not reach the database.
        self.assertEqual(options["creationflags"], 0x08000000 | 0x00000200)
        self.assertEqual(command[command.index("-t") + 1], "120")
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertNotIn(options["stdout"], (subprocess.PIPE, None))
        self.assertFalse(cluster.log_file.exists())         # pg_ctl makes that file, not pcldb
        block = (settings.pgdata_dir / "postgresql.conf").read_text(encoding="utf-8")
        self.assertIn("unix_socket_directories = ''", block)

    def test_elsewhere_there_are_no_creation_flags(self):
        project = support.TempProject("postgresql")
        self.addCleanup(project.cleanup)
        cluster = LocalCluster(project.settings)
        seen = []

        def fake_run(command, **options):
            seen.append(options.get("creationflags"))
            return subprocess.CompletedProcess(command, 0)

        for system in ("darwin", "linux"):
            with mock.patch.object(pgcluster, "current_system", return_value=system), \
                    mock.patch.object(pgcluster.subprocess, "run", fake_run):
                cluster._run(["pg_ctl", "start"], detached=True)
        self.assertEqual(seen, [0, 0])

    def test_initdb_without_access_to_the_folder_says_why_on_windows(self):
        """initdb on Windows runs with reduced rights: a folder that only administrators may
        open (made by an administrator's program) stops it with "Permission denied"."""
        project = support.TempProject("postgresql")
        self.addCleanup(project.cleanup)
        hint = "cannot use a folder that only administrators may open"

        def message(system, said):
            def stand_in_initdb(command, **options):
                options["stdout"].write(said.encode("utf-8"))
                return subprocess.CompletedProcess(command, 1)

            cluster = LocalCluster(project.settings, test_mode=True)
            cluster._bin = PgBin(project.path / "bin", "18.7", 18, 7, ".exe")
            with mock.patch.object(pgcluster, "current_system", return_value=system), \
                    mock.patch.object(pgcluster.subprocess, "run", stand_in_initdb):
                with self.assertRaises(PclError) as caught:
                    cluster.init()
            self.assertEqual(caught.exception.code, "postgres_failed")
            self.assertIn(said.strip(), str(caught.exception))
            return str(caught.exception)

        denied = ('initdb: error: could not open file "C:\\Temp\\pcldb pg x\\var\\'
                  '.initdb-password.1234abcd.tmp" for reading: Permission denied\n')
        text = message("windows", denied)
        self.assertIn(hint, text)
        self.assertIn("PostgreSQL's programs run with reduced rights", text)
        self.assertIn("your own user profile", text)
        self.assertTrue(text.endswith("run the command again."), text)
        for system, said in (("darwin", denied), ("linux", denied),
                             ("windows", "initdb: error: something else went wrong\n")):
            self.assertNotIn(hint, message(system, said), system)


FAILING_TESTS = """
import unittest
from tests import support


class Failing(support.PostgresCase, unittest.TestCase):
    def test_fails(self):
        cluster = support.shared_cluster()
        print("ROOT=%s" % cluster.root, flush=True)
        print("PORT=%d" % cluster.port, flush=True)
        print("PROJECT=%s" % self.project.path, flush=True)
        with self.store.app() as db:
            support.seed_minimal(db)
        self.fail("on purpose")

    def test_raises(self):
        raise RuntimeError("on purpose")


forgotten = support.TempProject("sqlite")
print("FORGOTTEN=%s" % forgotten.path, flush=True)
unittest.main(argv=["failing"])
"""

WAITING = """
import time
import unittest
from tests import support

class Waiting(support.PostgresCase, unittest.TestCase):
    def test_waits(self):
        cluster = support.shared_cluster()
        print("ROOT=%s" % cluster.root, flush=True)
        print("PORT=%d" % cluster.port, flush=True)
        print("PROJECT=%s" % self.project.path, flush=True)
        try:
            time.sleep(300)
        except BaseException:
            print("the test saw an exception", flush=True)
            raise


unittest.main(argv=["waiting"])
"""


def printed(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line[:12])


class TestSupport(unittest.TestCase):
    """tests/support.py itself: what the other test modules rely on."""

    def test_missing_postgresql_skips_or_fails_as_asked(self):
        with mock.patch.object(support, "_problem_checked", True), \
                mock.patch.object(support, "_problem", "the programs were not found"):
            with mock.patch.object(support, "REQUIRE_POSTGRES", False):
                with self.assertRaises(unittest.SkipTest) as skipped:
                    support.require_postgres()
                self.assertIn("the programs were not found", str(skipped.exception))
                with self.assertRaises(unittest.SkipTest):
                    support.shared_cluster()
            with mock.patch.object(support, "REQUIRE_POSTGRES", True):
                with self.assertRaises(AssertionError) as failed:
                    support.require_postgres()
                self.assertNotIsInstance(failed.exception, unittest.SkipTest)
                self.assertIn("PCLDB_REQUIRE_POSTGRES=1", str(failed.exception))
        with mock.patch.object(support, "_problem_checked", True), \
                mock.patch.object(support, "_problem", None):
            support.require_postgres()                      # nothing missing: nothing happens

    def test_a_wrong_pcldb_pg_bin_is_a_problem_not_another_version(self):
        with mock.patch.dict(os.environ, {"PCLDB_PG_BIN": tempfile.gettempdir()}), \
                mock.patch.object(support, "_problem_checked", False), \
                mock.patch.object(support, "_problem", None):
            problem = support.postgres_problem()
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.assertIn("psycopg", problem)
        else:
            self.assertIn("PCLDB_PG_BIN", problem)
            self.assertIn(tempfile.gettempdir(), problem)

    def test_the_shared_cluster_is_stopped_and_deleted_even_when_tests_fail(self):
        support.require_postgres()
        result = subprocess.run([sys.executable, "-B", "-c", FAILING_TESTS],
                                cwd=str(Path(__file__).resolve().parent.parent),
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAILED (failures=1, errors=1)", result.stderr)
        facts = printed(result.stdout)
        self.assertEqual(sorted(facts), ["FORGOTTEN", "PORT", "PROJECT", "ROOT"], result.stdout)
        self.assertIn("pcldb pg ", facts["ROOT"])
        for key in ("ROOT", "PROJECT", "FORGOTTEN"):
            self.assertFalse(Path(facts[key]).exists(), "%s was left behind" % key)
        self.assertFalse(util.port_answers(int(facts["PORT"])))
        self.assertNotIn(int(facts["PORT"]), (8787, 15439))

    @unittest.skipIf(os.name == "nt", "Windows ends a process without telling it")
    def test_the_shared_cluster_is_stopped_and_deleted_when_the_run_is_terminated(self):
        support.require_postgres()
        process = subprocess.Popen([sys.executable, "-B", "-c", WAITING],
                                   cwd=str(Path(__file__).resolve().parent.parent),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            facts = printed("".join(process.stdout.readline() for _ in range(3)))
            self.assertEqual(sorted(facts), ["PORT", "PROJECT", "ROOT"])
            self.assertTrue(Path(facts["ROOT"]).is_dir())
            self.assertTrue(Path(facts["PROJECT"]).is_dir())
            self.assertTrue(util.port_answers(int(facts["PORT"])))
            process.terminate()                     # in the middle of a running test
            process.wait(120)
            self.assertEqual(process.stdout.read(), "")
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(process.returncode, -15)           # ended by the signal, as asked
        self.assertFalse(Path(facts["ROOT"]).exists())
        self.assertFalse(Path(facts["PROJECT"]).exists())
        self.assertFalse(util.port_answers(int(facts["PORT"])))

    def test_temp_project(self):
        with support.TempProject("sqlite", {"sql.max_rows": 3}) as project:
            self.assertTrue(project.path.is_dir())
            self.assertIn(" ", project.path.name)
            self.assertEqual(project.settings.project_dir, project.path)
            self.assertEqual(project.settings.sql.max_rows, 3)
            self.assertEqual(project.settings.database.engine, "sqlite")
            (project.path / "var").mkdir()
            read_only = project.path / "var" / "locked.txt"
            read_only.write_text("x", encoding="utf-8")
            os.chmod(read_only, stat.S_IREAD)
        self.assertFalse(project.path.exists())             # read-only files go too
        project.cleanup()                                   # twice is fine
        with self.assertRaises(PclError):
            support.TempProject("sqlite", {"no.such": 1})
        self.assertEqual(support.free_port, util.free_port)
        self.assertIsInstance(Settings.for_test(tempfile.gettempdir()), Settings)

    def test_temporary_folders_on_windows_are_made_without_a_mode(self):
        check_temp_folder_helper(self, support.make_temp_folder,
                                 lambda: mock.patch.object(os, "name", "nt"),
                                 lambda: mock.patch.object(os, "name", "posix"))

    def test_the_folders_that_may_hold_a_cluster_come_from_the_helper(self):
        made = []

        def recording(prefix, windows=None):
            path = real_helper(prefix, windows)
            made.append((prefix, path))
            return path

        real_helper = support.make_temp_folder
        with mock.patch.object(support, "make_temp_folder", recording):
            support.TempProject("postgresql").cleanup()
            with mock.patch.object(support.Settings, "for_test",
                                   side_effect=RuntimeError("stop here")):
                with self.assertRaises(RuntimeError):
                    support._SharedCluster()
        self.assertEqual([prefix for prefix, _path in made], ["pcldb test ", "pcldb pg "])
        for _prefix, path in made:
            self.assertFalse(Path(path).exists(), path)


def check_temp_folder_helper(case, helper, as_windows, as_elsewhere):
    """The checks for tests/support.py's and tools/smoke.py's make_temp_folder.

    Windows (Python 3.13 and newer): tempfile.mkdtemp's mode 0o700 becomes rights that only
    SYSTEM, Administrators and the owner have, and initdb (reduced rights) is then refused. So on
    Windows the folder is made with a plain os.mkdir(path): no mode at all."""
    base = Path(tempfile.mkdtemp(prefix="pcldb helper "))
    case.addCleanup(support.remove_tree, base)
    real_mkdir = os.mkdir
    calls = []

    def recording_mkdir(path, *args, **kwargs):
        calls.append((path, args, kwargs))
        if len(calls) == 1:
            raise FileExistsError(path)         # a name that is taken: the next one is tried
        return real_mkdir(path)

    def no_mkdtemp(*_args, **_kwargs):
        raise AssertionError("tempfile.mkdtemp was used on Windows (it gives mode 0o700)")

    for windows in (True, None):                # None: the helper looks at the system itself
        calls.clear()
        with mock.patch.object(tempfile, "gettempdir", return_value=str(base)), \
                mock.patch.object(tempfile, "mkdtemp", no_mkdtemp), \
                mock.patch.object(os, "mkdir", recording_mkdir), \
                (as_windows() if windows is None else contextlib.nullcontext()):
            text = helper("pcldb pg ", windows) if windows else helper("pcldb pg ")
        made = Path(text)                       # (not while os.name is changed: Path follows it)
        case.assertTrue(made.is_dir())
        case.assertEqual(made.parent, base)
        case.assertTrue(made.name.startswith("pcldb pg "), made.name)
        case.assertGreaterEqual(len(made.name), len("pcldb pg ") + 8)
        case.assertEqual(len(calls), 2)
        for path, args, kwargs in calls:
            case.assertEqual((args, kwargs), ((), {}), "os.mkdir got a mode")
            case.assertTrue(os.path.basename(path).startswith("pcldb pg "))
        case.assertEqual(calls[1][0], str(made))

    # Everywhere else: tempfile.mkdtemp, as before.
    for windows in (False, None):
        with mock.patch.object(tempfile, "gettempdir", return_value=str(base)), \
                (as_elsewhere() if windows is None else contextlib.nullcontext()):
            text = helper("pcldb pg ", False) if windows is False else helper("pcldb pg ")
        made = Path(text)
        case.assertTrue(made.is_dir())
        case.assertTrue(made.name.startswith("pcldb pg "), made.name)
        if os.name != "nt":
            case.assertEqual(stat.S_IMODE(made.stat().st_mode), 0o700)
        support.remove_tree(made)


if __name__ == "__main__":
    unittest.main()
