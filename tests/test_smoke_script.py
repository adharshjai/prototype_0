"""tools/smoke.py and .github/workflows/ci.yml (specification section 14).

- the smoke script's helpers (environment, ports, output, failure report) and one real run of
  it on SQLite in a temporary folder (PostgreSQL's run is the CI's job, and test_end_to_end's);
- the CI file, read with a small parser for the part of YAML it is written in (no PyYAML in
  the standard library): the events, permissions, concurrency, the matrix, the pinned actions,
  the pip cache, the timeouts, PCLDB_PG_BIN per image, the steps the specification asks for,
  and every file a "run" step names exists.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "ci.yml"
SMOKE = ROOT / "tools" / "smoke.py"

sys.path.insert(0, str(ROOT / "tools"))
try:
    import smoke        # noqa: E402  (tools/ is not a package)
finally:
    sys.path.remove(str(ROOT / "tools"))


# ---------------------------------------------------------------------- a small YAML reader


class YamlError(ValueError):
    pass


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _scalar(text, number):
    text = text.strip()
    if not text:
        return None
    if text[0] == '"':
        if not text.endswith('"') or len(text) < 2:
            raise YamlError("line %d: unclosed double quote" % number)
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if text[0] == "'":
        if not text.endswith("'") or len(text) < 2:
            raise YamlError("line %d: unclosed single quote" % number)
        return text[1:-1].replace("''", "'")
    if text[0] in "[{&*!%@`>":
        raise YamlError("line %d: this reader does not take %r" % (number, text[0]))
    if " #" in text:
        raise YamlError("line %d: no comments after a value, please" % number)
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _split_key(text, number):
    match = re.match(r"^([A-Za-z0-9_.${}\- ]+?):(?:\s+(.*))?$", text)
    if not match:
        raise YamlError("line %d: expected 'key: value', got %r" % (number, text))
    return match.group(1), match.group(2)


def load_yaml(text):
    """Block mappings, block sequences, plain / quoted scalars and `|` literal blocks: exactly
    what ci.yml uses. Anything else raises YamlError."""
    if "\t" in text:
        raise YamlError("tabs are not allowed in YAML indentation")
    lines = [(number, line.rstrip("\n")) for number, line in enumerate(text.split("\n"), 1)]

    def meaningful(i):
        while i < len(lines):
            stripped = lines[i][1].strip()
            if stripped and not stripped.startswith("#"):
                return i
            i += 1
        return i

    def literal(i, parent_indent):
        """A `|` block: the following lines indented more than the key."""
        body = []
        block_indent = None
        while i < len(lines):
            number, line = lines[i]
            if line.strip() and _indent(line) <= parent_indent:
                break
            if line.strip() and block_indent is None:
                block_indent = _indent(line)
            body.append(line[block_indent:] if line.strip() else "")
            i += 1
        while body and not body[-1]:
            body.pop()
        return "\n".join(body) + "\n", i

    def value_after_key(rest, i, indent, number):
        if rest is None or rest == "":
            j = meaningful(i)
            if j < len(lines) and _indent(lines[j][1]) > indent:
                return block(j, _indent(lines[j][1]))
            if j < len(lines) and _indent(lines[j][1]) == indent and \
                    lines[j][1].strip().startswith("- "):
                return block(j, indent)
            return None, i
        if rest.strip() == "|":
            return literal(i, indent)
        return _scalar(rest, number), i

    def mapping(i, indent, first=None):
        result = {}
        if first is not None:
            key, rest, number, i = first
            result[key], i = value_after_key(rest, i, indent, number)
        while True:
            i = meaningful(i)
            if i >= len(lines):
                return result, i
            number, line = lines[i]
            if _indent(line) < indent:
                return result, i
            if _indent(line) > indent:
                raise YamlError("line %d: unexpected indentation" % number)
            text = line.strip()
            if text.startswith("- "):
                return result, i
            key, rest = _split_key(text, number)
            if key in result:
                raise YamlError("line %d: the key %r appears twice" % (number, key))
            result[key], i = value_after_key(rest, i + 1, indent, number)

    def sequence(i, indent):
        result = []
        while True:
            i = meaningful(i)
            if i >= len(lines):
                return result, i
            number, line = lines[i]
            if _indent(line) != indent or not line.strip().startswith("- "):
                if _indent(line) > indent:
                    raise YamlError("line %d: unexpected indentation" % number)
                return result, i
            text = line.strip()[2:]
            item_indent = indent + 2
            if re.match(r"^[A-Za-z0-9_.\-]+:(\s|$)", text):
                key, rest = _split_key(text, number)
                item, i = mapping(i + 1, item_indent, first=(key, rest, number, i + 1))
            else:
                item, i = _scalar(text, number), i + 1
            result.append(item)

    def block(i, indent):
        number, line = lines[i]
        if line.strip().startswith("- "):
            return sequence(i, indent)
        return mapping(i, indent)

    start = meaningful(0)
    if start >= len(lines):
        return {}
    value, end = block(start, _indent(lines[start][1]))
    end = meaningful(end)
    if end < len(lines):
        raise YamlError("line %d: not read (wrong indentation?)" % lines[end][0])
    return value


class TestYamlReader(unittest.TestCase):

    def test_reads_the_shapes_ci_uses(self):
        text = ("# a comment\n"
                "name: ci\n"
                "on:\n  push:\n  workflow_dispatch:\n"
                "env:\n  A: \"1\"\n  B: 'it''s'\n"
                "jobs:\n  one:\n    timeout-minutes: 25\n    steps:\n"
                "      - name: First\n        run: |\n          echo a\n\n          echo b\n"
                "      - uses: x/y@v7\n        with:\n          k: v\n"
                "    list:\n      - a\n      - b\n")
        self.assertEqual(load_yaml(text), {
            "name": "ci", "on": {"push": None, "workflow_dispatch": None},
            "env": {"A": "1", "B": "it's"},
            "jobs": {"one": {"timeout-minutes": 25, "steps": [
                {"name": "First", "run": "echo a\n\necho b\n"},
                {"uses": "x/y@v7", "with": {"k": "v"}}], "list": ["a", "b"]}}})

    def test_refuses_what_it_cannot_read(self):
        for text in ("a: [1, 2]\n", "a:\n\tb: 1\n", "a: &x 1\n", "a: 1\n  b: 2\n",
                     "a: 1\na: 2\n", "a: 'open\n", "a: b # note\n"):
            with self.subTest(text=text), self.assertRaises(YamlError):
                load_yaml(text)


# ---------------------------------------------------------------------- the CI file


def ci():
    return load_yaml(CI_FILE.read_text(encoding="utf-8"))


def all_steps(workflow):
    for job_name, job in workflow["jobs"].items():
        for step in job["steps"]:
            yield job_name, step


class TestCiFile(unittest.TestCase):

    def setUp(self):
        self.workflow = ci()
        self.jobs = self.workflow["jobs"]

    def test_plain_ascii_lf_no_tabs(self):
        data = CI_FILE.read_bytes()
        self.assertTrue(data.isascii())
        self.assertNotIn(b"\r", data)
        self.assertNotIn(b"\t", data)
        self.assertTrue(data.endswith(b"\n"))

    def test_events_permissions_concurrency(self):
        self.assertEqual(set(self.workflow["on"]), {"push", "pull_request", "workflow_dispatch"})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        concurrency = self.workflow["concurrency"]
        self.assertIs(concurrency["cancel-in-progress"], True)
        self.assertIn("github.ref", concurrency["group"])

    def test_the_matrix_and_the_hand_started_macos_job(self):
        self.assertEqual(set(self.jobs), {"test", "macos"})
        include = self.jobs["test"]["strategy"]["matrix"]["include"]
        self.assertEqual([(row["os"], row["python"]) for row in include],
                         [("windows-latest", "3.11"), ("windows-latest", "3.14"),
                          ("ubuntu-26.04", "3.13")])
        self.assertIs(self.jobs["test"]["strategy"]["fail-fast"], False)
        self.assertNotIn("if", self.jobs["test"])
        macos = self.jobs["macos"]
        self.assertEqual(macos["runs-on"], "macos-latest")
        self.assertEqual(macos["if"], "github.event_name == 'workflow_dispatch'")

    def test_every_job_has_the_timeout(self):
        for name, job in self.jobs.items():
            self.assertEqual(job.get("timeout-minutes"), 25, name)

    def test_actions_are_pinned_to_the_current_major_versions(self):
        # research_postgres_local.md D.4 and the actions' own README today: v7 for both.
        wanted = {"actions/checkout": "v7", "actions/setup-python": "v7"}
        used = set()
        for job, step in all_steps(self.workflow):
            if "uses" in step:
                action, _, version = step["uses"].partition("@")
                self.assertEqual(version, wanted.get(action), "%s in %s" % (step["uses"], job))
                used.add(action)
                if action == "actions/setup-python":
                    self.assertEqual(step["with"]["cache"], "pip")
                    self.assertEqual(step["with"]["cache-dependency-path"].split(),
                                     ["requirements.txt", "requirements-globus.txt"])
                if action == "actions/checkout":
                    self.assertIs(step["with"]["persist-credentials"], False)
        self.assertEqual(used, set(wanted))

    def test_the_python_versions_come_from_the_matrix_or_are_3_13(self):
        for job, step in all_steps(self.workflow):
            if step.get("uses", "").startswith("actions/setup-python"):
                expected = "${{ matrix.python }}" if job == "test" else "3.13"
                self.assertEqual(step["with"]["python-version"], expected)

    def runs(self, job):
        return [step for step in self.jobs[job]["steps"] if "run" in step]

    def test_postgresql_programs_are_found_through_pcldb_pg_bin_per_image(self):
        by_os = {}
        for step in self.runs("test") + self.runs("macos"):
            if "PCLDB_PG_BIN=" in step["run"]:
                by_os[step.get("if", "macos")] = step["run"]
        self.assertEqual(set(by_os), {"runner.os == 'Windows'", "runner.os == 'Linux'", "macos"})
        self.assertIn("$env:PGBIN", by_os["runner.os == 'Windows'"])
        self.assertIn("/usr/lib/postgresql/*/bin", by_os["runner.os == 'Linux'"])
        self.assertIn("sort -V | tail -n 1", by_os["runner.os == 'Linux'"])
        self.assertIn("brew install postgresql@18", by_os["macos"])
        self.assertIn("brew --prefix postgresql@18", by_os["macos"])
        for text in by_os.values():
            self.assertIn("GITHUB_ENV", text)

    def test_the_image_database_service_is_never_started(self):
        for job, step in all_steps(self.workflow):
            text = step.get("run", "")
            for word in ("systemctl", "Start-Service", "Set-Service", "net start", "pg_ctl ",
                         "brew services", "service postgresql", "pg_ctlcluster"):
                self.assertNotIn(word, text, "%s: %s" % (job, step.get("name")))
        self.assertNotIn("services", self.jobs["test"])
        self.assertNotIn("services", self.jobs["macos"])

    def test_the_steps_the_specification_asks_for_in_both_jobs(self):
        for job in ("test", "macos"):
            texts = [step["run"] for step in self.runs(job)]
            joined = "\n".join(texts)
            self.assertIn("python -m pip install -r requirements.txt -r requirements-globus.txt",
                          joined)
            suite = [step for step in self.runs(job)
                     if step["run"].strip() == "python -B -m unittest discover -s tests -t ."]
            self.assertEqual(len(suite), 1, job)
            self.assertEqual(suite[0]["env"], {"PCLDB_REQUIRE_POSTGRES": "1"})
            self.assertIn("python -B tools/smoke.py --engine both", joined)
            wrong = [step for step in self.runs(job) if "PGPASSWORD" in step.get("env", {})]
            self.assertEqual(len(wrong), 1, job)
            env = wrong[0]["env"]
            self.assertEqual(env["PCLDB_REQUIRE_POSTGRES"], "1")
            for name in ("PGUSER", "PGPASSWORD", "PGDATA", "PGHOST", "PGPORT", "PGDATABASE"):
                self.assertTrue(env.get(name), name)
            self.assertIn("tests.test_pgcluster", wrong[0]["run"])
            self.assertIn("tests.test_end_to_end", wrong[0]["run"])
            bare = [step for step in self.runs(job) if "--without-pip" in step["run"]]
            self.assertEqual(len(bare), 1, job)
            self.assertNotIn("pip install", bare[0]["run"])
            self.assertIn("tools/smoke.py --engine sqlite", bare[0]["run"])
            self.assertIn("import $package", bare[0]["run"])
            self.assertNotIn("PCLDB_REQUIRE_POSTGRES", bare[0].get("env", {}))

    def test_both_jobs_run_the_same_test_steps(self):
        def tests_part(job):
            return [step["run"] for step in self.runs(job) if "PCLDB_PG_BIN=" not in step["run"]]
        self.assertEqual(tests_part("test"), tests_part("macos"))

    def test_every_file_a_run_step_names_exists(self):
        named = set()
        for job, step in all_steps(self.workflow):
            text = step.get("run", "")
            for match in re.finditer(r"(?<![\w$/.-])((?:[\w-]+/)*[\w.-]+\.(?:py|txt|json|cmd|sh))\b",
                                     text):
                named.add(match.group(1))
            for match in re.finditer(r"\btests\.(test_\w+)", text):
                named.add("tests/%s.py" % match.group(1))
            for match in re.finditer(r"-s (\w+) -t \.", text):
                named.add(match.group(1) + "/")
        self.assertTrue({"requirements.txt", "requirements-globus.txt", "tools/smoke.py",
                         "tests/test_pgcluster.py", "tests/"} <= named, named)
        missing = sorted(name for name in named if not (ROOT / name).exists())
        self.assertEqual(missing, [])

    def test_shells_are_named_where_the_default_would_differ(self):
        for job, step in all_steps(self.workflow):
            text = step.get("run", "")
            if "$env:" in text or "$LASTEXITCODE" in text:
                self.assertEqual(step.get("shell"), "pwsh", step.get("name"))
            if "$GITHUB_ENV" in text or "$(" in text:
                self.assertEqual(step.get("shell"), "bash", step.get("name"))


# ---------------------------------------------------------------------- the smoke script


class TestSmokeHelpers(unittest.TestCase):

    def test_the_environment_keeps_only_what_it_should(self):
        base = {"PATH": "/bin", "PCLDB_DIR": "/somewhere", "PCLDB_TOKEN": "pcl_secret",
                "PCLDB_ENGINE": "postgresql", "PCLDB_PG_BIN": "/pg/bin", "pcldb_llm_api_key": "x",
                "PGUSER": "someone", "PYTHONPATH": "/elsewhere"}
        env = smoke.child_environment(base, Path("p"), Path("u"), code_root=Path("code"))
        self.assertEqual(env["PCLDB_DIR"], "p")
        self.assertEqual(env["PCLDB_USER_DIR"], "u")
        self.assertEqual(env["PCLDB_PG_BIN"], "/pg/bin")
        self.assertEqual(env["PYTHONPATH"], "code")
        self.assertEqual(env["PGUSER"], "someone")        # on purpose: pcldb must ignore it
        self.assertEqual(env["PCLDB_LLM_BASE_URL"], "http://127.0.0.1:9/api/v1")
        for name in ("PCLDB_TOKEN", "PCLDB_ENGINE", "pcldb_llm_api_key"):
            self.assertNotIn(name, env)

    def test_free_ports_are_never_the_defaults(self):
        ports = {smoke.free_port() for _ in range(20)}
        self.assertEqual(len(ports), 20)
        self.assertFalse(ports & {8787, 15439})
        self.assertTrue(all(20000 <= port < 49000 for port in ports))

    def test_the_open_line(self):
        code = "A" * 32
        found = smoke.OPEN_LINE.match("Open http://127.0.0.1:31234/#signin=%s" % code)
        self.assertEqual((found.group(1), found.group(2), found.group(3)),
                         ("http://127.0.0.1:31234/", "31234", code))
        self.assertIsNone(smoke.OPEN_LINE.match("Open http://evil.example/#signin=%s" % code))

    def test_find_json_takes_the_first_object(self):
        self.assertEqual(smoke.find_json('note\n{"a": 1}\nAnswered by: x\n'), {"a": 1})
        with self.assertRaises(ValueError):
            smoke.find_json("no json here")

    def test_output_is_ascii(self):
        self.assertEqual(smoke.ascii_text("caf\u00e9\x1b[31m"), "caf\\xe9 [31m")

    def test_tail_file_reads_the_end(self):
        with tempfile.TemporaryDirectory(prefix="pcldb smoke test ") as folder:
            path = Path(folder) / "a log.txt"
            path.write_text("".join("line %d\n" % n for n in range(100)), encoding="utf-8")
            self.assertEqual(smoke.tail_file(path), ["line %d" % n for n in range(60, 100)])
            self.assertEqual(smoke.tail_file(Path(folder) / "missing"), [])

    def test_a_failed_step_prints_its_output_and_logs(self):
        with tempfile.TemporaryDirectory(prefix="pcldb smoke test ") as folder:
            log = Path(folder) / "server.log"
            log.write_text("first\nthe log's last line\n", encoding="utf-8")
            engine = smoke.EngineSmoke.__new__(smoke.EngineSmoke)
            engine.engine = "sqlite"
            engine.results = []

            def failing():
                raise smoke.StepFailed("exit code 1 (wanted 0)", "out 1\nout 2\n", [log])
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                ok = engine.run_step("doctor", failing)
        self.assertFalse(ok)
        text = printed.getvalue()
        self.assertIn("[sqlite] FAIL  doctor: exit code 1 (wanted 0)", text)
        self.assertIn("    | out 2", text)
        self.assertIn("    | the log's last line", text)
        self.assertEqual(engine.results, [("doctor", False)])

    def test_remove_tree_removes_read_only_files(self):
        folder = Path(tempfile.mkdtemp(prefix="pcldb smoke test "))
        (folder / "inner").mkdir()
        target = folder / "inner" / "read only.txt"
        target.write_text("x", encoding="utf-8")
        os.chmod(target, 0o444)
        self.assertTrue(smoke.remove_tree(folder))
        self.assertFalse(folder.exists())

    def test_temporary_folders_on_windows_are_made_without_a_mode(self):
        from unittest import mock
        from tests.test_pgcluster import check_temp_folder_helper
        check_temp_folder_helper(self, smoke.make_temp_folder,
                                 lambda: mock.patch.object(smoke, "IS_WINDOWS", True),
                                 lambda: mock.patch.object(smoke, "IS_WINDOWS", False))

    def test_the_smoke_folder_comes_from_the_helper(self):
        from unittest import mock
        made = []
        real_helper = smoke.make_temp_folder

        def recording(prefix, windows=None):
            path = real_helper(prefix, windows)
            made.append(path)
            return path

        with mock.patch.object(smoke, "make_temp_folder", recording):
            engine = smoke.EngineSmoke("postgresql")
        self.addCleanup(smoke.remove_tree, engine.root)
        self.assertEqual([Path(path) for path in made], [engine.root])
        self.assertTrue(engine.root.name.startswith("pcldb smoke postgresql "))


class TestSmokeRun(unittest.TestCase):

    def test_the_sqlite_smoke_passes(self):
        env = {name: value for name, value in os.environ.items()
               if not name.startswith("PCLDB_") or name == "PCLDB_PG_BIN"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        done = subprocess.run([sys.executable, "-B", str(SMOKE), "--engine", "sqlite"],
                              cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        output = done.stdout.decode("utf-8", "replace")
        self.assertEqual(done.returncode, 0, output)
        self.assertTrue(output.isascii(), output)
        self.assertIn("smoke result: sqlite PASS", output)
        for step in ("setup --engine sqlite --demo", "doctor", "sql", "ask --no-llm",
                     "serve in the background", "stop serve like Ctrl+C", "db stop",
                     "nothing left running", "launcher", "remove the folder"):
            self.assertIn("[sqlite] PASS  " + step, output)
        folder = re.search(r"\[sqlite\] folder: (.+)", output).group(1).strip()
        self.assertFalse(Path(folder).exists(), folder)
        self.assertNotIn("8787", output)
        self.assertNotIn("15439", output)

    def test_help(self):
        done = subprocess.run([sys.executable, "-B", str(SMOKE), "--help"], cwd=str(ROOT),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)
        self.assertEqual(done.returncode, 0)
        self.assertIn(b"--engine", done.stdout)


if __name__ == "__main__":
    unittest.main()
