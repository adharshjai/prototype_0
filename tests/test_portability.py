"""Windows hazards, found by reading the code base, turned into tests (specification 1.2, 14).

Every check here reads the source with `ast` (or looks at file names and bytes), so it fails on
macOS and Linux too, before the first run on Windows would. What is checked:

- no `os.fork` and friends, no `preexec_fn`, no `shell=True`, no "/tmp" literals;
- POSIX-only signals (SIGKILL, SIGHUP, SIGQUIT, ...) and Windows-only ones (SIGBREAK,
  CTRL_BREAK_EVENT) only behind a guard (`os.name`, `sys.platform`, `hasattr`, a try that
  catches AttributeError) or through `getattr(signal, NAME, None)`;
- every text-mode `open()`, `Path.open()`, `read_text()`, `write_text()`, text-mode subprocess
  call and `TextIOWrapper` names its encoding (Windows would use the ANSI code page);
- modules that store paths never build them with the system's separator (`os.sep`,
  `normpath`, `str(x.relative_to(...))` give backslashes on Windows);
- `pg_ctl start` is never run with a pipe (the server inherits it and the call never returns
  on Windows): pipes appear only in the two allowed helpers of pgcluster.py, and a recorded
  start shows DEVNULL / a file;
- PostgreSQL's programs are always named through `PgBin.program()` (which adds `.exe`);
- the string literals of everything that writes to the console are ASCII;
- file names in the repository: no two differ only by letter case, none is invalid on
  Windows (reserved names, `<>:"|?*`, a trailing dot or space), none is too long.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", ".venv", "var", "__pycache__", "node_modules", ".mypy_cache",
             ".pytest_cache", ".idea", ".vscode"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", "pcldb_config.json"}


def repository_files():
    """Every file that belongs in the repository (git-ignored local state left out)."""
    found = []
    for folder, directories, files in os.walk(ROOT):
        directories[:] = sorted(name for name in directories if name not in SKIP_DIRS)
        for name in sorted(files):
            if name in SKIP_FILES or name.endswith((".pyc", ".pyo")):
                continue
            found.append(Path(folder) / name)
    return found


def code_files():
    """The program: the package, the installer and the tools."""
    files = sorted((ROOT / "pcldb").rglob("*.py"))
    files.append(ROOT / "install.py")
    files += sorted((ROOT / "tools").glob("*.py"))
    return [path for path in files if path.is_file()]


def test_files():
    return sorted((ROOT / "tests").glob("*.py"))


def python_files():
    return code_files() + test_files()


_parsed = {}


def parse(path):
    if path not in _parsed:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node
        _parsed[path] = tree
    return _parsed[path]


def where(path, node):
    return "%s:%d" % (path.relative_to(ROOT).as_posix(), getattr(node, "lineno", 0))


def dotted(node):
    """'os.path.join' for the expression os.path.join, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def keyword(call, name):
    for item in call.keywords:
        if item.arg == name:
            return item.value
    return None


def is_true(node):
    return isinstance(node, ast.Constant) and node.value is True


def ancestors(node):
    while hasattr(node, "parent"):
        node = node.parent
        yield node


GUARD_WORDS = ("os.name", "sys.platform", "hasattr(", "current_system", "platform.system",
               "IS_WINDOWS", "ON_WINDOWS", "WINDOWS")


def is_guarded(node):
    """True when the node only runs on the system that has it: inside an if (or a skip
    decorator) that asks which system this is, or a try that catches AttributeError."""
    for parent in ancestors(node):
        if isinstance(parent, (ast.If, ast.IfExp, ast.While)):
            if any(word in ast.unparse(parent.test) for word in GUARD_WORDS):
                return True
        if isinstance(parent, ast.Try):
            for handler in parent.handlers:
                caught = ast.unparse(handler.type) if handler.type is not None else "BaseException"
                if any(name in caught for name in ("AttributeError", "Exception")):
                    return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in parent.decorator_list:
                text = ast.unparse(decorator)
                if "skip" in text and any(word in text for word in GUARD_WORDS):
                    return True
    return False


# Places that look like a hazard but are not one, each with the reason (file, function).
ALLOWED = {
    ("pcldb/store/reader_sqlite.py", "temp_folder"):
        "mirrors SQLite's own Unix search order; Windows returns earlier (os.name == 'nt')",
    ("pcldb/transfer.py", "_same_file"):
        "compares two files on disk (never stored); normcase is right for that on Windows",
}
THIS_FILE = Path(__file__).resolve()


def allowed(path, node):
    function = None
    for parent in ancestors(node):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = parent.name
            break
    return (path.relative_to(ROOT).as_posix(), function) in ALLOWED


class TestNoPosixOnlyCalls(unittest.TestCase):

    FORBIDDEN_CALLS = {"os.fork", "os.forkpty", "os.setsid", "os.setpgrp", "os.killpg",
                       "os.getpgid", "os.getuid", "os.geteuid", "os.getgid", "os.chown",
                       "os.mkfifo", "os.wait3", "os.wait4", "pty.spawn", "pty.fork"}

    def test_no_fork_and_no_posix_only_os_functions(self):
        found = []
        for path in python_files():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Attribute) and dotted(node) in self.FORBIDDEN_CALLS \
                        and not is_guarded(node):
                    found.append("%s %s" % (where(path, node), dotted(node)))
        self.assertEqual(found, [], "POSIX-only functions used without a guard")

    def test_no_preexec_fn_and_no_shell(self):
        found = []
        for path in python_files():
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                if keyword(node, "preexec_fn") is not None:
                    found.append("%s preexec_fn" % where(path, node))
                if is_true(keyword(node, "shell")):
                    found.append("%s shell=True" % where(path, node))
                if dotted(node.func) in ("os.system", "os.popen", "subprocess.getoutput",
                                         "subprocess.getstatusoutput"):
                    found.append("%s %s (runs a shell)" % (where(path, node), dotted(node.func)))
        self.assertEqual(found, [])

    def test_no_tmp_literals(self):
        found = []
        for path in python_files():
            if path == THIS_FILE:
                continue
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and not allowed(path, node):
                    text = node.value
                    if text in ("/tmp", "/var/tmp", "/private/tmp") or \
                            text.startswith(("/tmp/", "/var/tmp/", "/private/tmp/")):
                        found.append("%s %r" % (where(path, node), text))
        self.assertEqual(found, [], "use tempfile instead of a /tmp literal")


class TestSignals(unittest.TestCase):

    POSIX_ONLY = {"SIGKILL", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGCHLD",
                  "SIGPIPE", "SIGCONT", "SIGSTOP", "SIGTSTP", "SIGTTIN", "SIGTTOU", "SIGWINCH",
                  "alarm", "setitimer", "getitimer", "pause", "siginterrupt", "pthread_kill",
                  "pthread_sigmask", "sigwait", "sigtimedwait", "sigpending"}
    WINDOWS_ONLY = {"SIGBREAK", "CTRL_C_EVENT", "CTRL_BREAK_EVENT"}

    def test_system_specific_signals_are_guarded(self):
        found = []
        for path in python_files():
            for node in ast.walk(parse(path)):
                name = dotted(node) if isinstance(node, ast.Attribute) else None
                if not name or not name.startswith("signal."):
                    continue
                attribute = name.split(".", 1)[1]
                if attribute in self.POSIX_ONLY | self.WINDOWS_ONLY and not is_guarded(node):
                    found.append("%s %s" % (where(path, node), name))
        self.assertEqual(found, [], "a signal that one system lacks, used without a guard "
                                    "(use getattr(signal, NAME, None) or check os.name)")

    def test_guard_detection_itself(self):
        tree = ast.parse("import os, signal\n"
                         "a = signal.SIGHUP\n"
                         "if os.name == 'nt':\n    b = signal.CTRL_BREAK_EVENT\n"
                         "try:\n    c = signal.SIGKILL\nexcept AttributeError:\n    pass\n")
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node
        guarded = {dotted(node): is_guarded(node) for node in ast.walk(tree)
                   if isinstance(node, ast.Attribute) and dotted(node)
                   and dotted(node).startswith("signal.")}
        self.assertEqual(guarded, {"signal.SIGHUP": False, "signal.CTRL_BREAK_EVENT": True,
                                   "signal.SIGKILL": True})


# ---------------------------------------------------------------------- encodings


def text_mode_without_encoding(call):
    """A reason when this call opens a file or a process in text mode without naming the
    encoding (Windows would use the ANSI code page), else None."""
    name = dotted(call.func) or ""
    func = call.func
    has_encoding = keyword(call, "encoding") is not None

    def mode_problem(mode, default_text=True):
        if mode is None:
            return "default text mode" if default_text else None
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return None if "b" in mode.value else "mode %r" % mode.value
        return "a mode that is not a literal"

    if isinstance(func, ast.Name) and func.id == "open" or name in ("io.open", "codecs.open"):
        mode = call.args[1] if len(call.args) > 1 else keyword(call, "mode")
        problem = mode_problem(mode)
        return None if problem is None or has_encoding else "open() with %s" % problem
    if isinstance(func, ast.Attribute) and func.attr == "open" and name not in (
            "webbrowser.open", "os.open", "tarfile.open", "zipfile.open", "gzip.open"):
        # Path.open(mode); other objects' open(...) take something else first.
        if call.args and not (isinstance(call.args[0], ast.Constant)
                              and isinstance(call.args[0].value, str)):
            return None
        mode = call.args[0] if call.args else keyword(call, "mode")
        problem = mode_problem(mode)
        return None if problem is None or has_encoding else ".open() with %s" % problem
    if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
        if func.attr == "read_text" and call.args or func.attr == "write_text" and \
                len(call.args) > 1:
            return None         # encoding given by position
        return None if has_encoding else "%s() without encoding" % func.attr
    if name.split(".")[-1] in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"):
        mode = call.args[0] if call.args else keyword(call, "mode")
        problem = mode_problem(mode, default_text=False)
        return None if problem is None or has_encoding else "%s with %s" % (name, problem)
    if name.split(".")[-1] == "TextIOWrapper":
        return None if has_encoding else "TextIOWrapper without encoding"
    if name in ("subprocess.run", "subprocess.Popen", "subprocess.check_output",
                "subprocess.call", "subprocess.check_call"):
        text = keyword(call, "text") or keyword(call, "universal_newlines")
        if text is not None and not (isinstance(text, ast.Constant) and not text.value):
            return None if has_encoding else "%s in text mode without encoding" % name
    return None


class TestEncodings(unittest.TestCase):

    def test_every_text_file_and_text_process_names_its_encoding(self):
        found = []
        for path in python_files():
            in_tests = path.parent.name == "tests"
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Call):
                    problem = text_mode_without_encoding(node)
                    if problem and in_tests and problem.startswith("subprocess."):
                        # A test that reads a child Python's own ASCII output: the locale's
                        # code page decodes ASCII the same. The package itself must say it.
                        continue
                    if problem:
                        found.append("%s %s" % (where(path, node), problem))
        self.assertEqual(found, [], "name the encoding (utf-8) or open in binary mode")

    def test_the_checker_itself(self):
        def check(source):
            return text_mode_without_encoding(ast.parse(source).body[0].value)
        self.assertIsNotNone(check("open(p)"))
        self.assertIsNotNone(check("open(p, 'w')"))
        self.assertIsNone(check("open(p, 'rb')"))
        self.assertIsNone(check("open(p, 'w', encoding='utf-8')"))
        self.assertIsNotNone(check("path.open()"))
        self.assertIsNone(check("path.open('ab')"))
        self.assertIsNone(check("opener.open(request, timeout=2)"))
        self.assertIsNotNone(check("path.read_text()"))
        self.assertIsNone(check("path.read_text(encoding='utf-8')"))
        self.assertIsNotNone(check("path.write_text(text)"))
        self.assertIsNotNone(check("subprocess.run(args, text=True)"))
        self.assertIsNone(check("subprocess.run(args, text=True, encoding='utf-8')"))
        self.assertIsNone(check("subprocess.run(args, capture_output=True)"))
        self.assertIsNotNone(check("tempfile.NamedTemporaryFile('w')"))
        self.assertIsNone(check("tempfile.NamedTemporaryFile()"))


# ---------------------------------------------------------------------- stored paths


# The modules that turn folders and documents into the path text that is stored and hashed
# (the Search subject is a hash of it: a backslash would make another subject).
PATH_STORING = ("registration.py", "sources.py", "adapters_raman.py", "reconcile.py", "demo.py",
                "search_entries.py", "transfer.py")


class TestStoredPaths(unittest.TestCase):

    def test_no_system_separator_in_modules_that_store_paths(self):
        found = []
        for name in PATH_STORING:
            path = ROOT / "pcldb" / name
            for node in ast.walk(parse(path)):
                text = dotted(node) if isinstance(node, ast.Attribute) else None
                if text in ("os.sep", "os.path.sep", "os.altsep", "os.path.altsep",
                            "os.path.normpath", "os.path.normcase", "os.path.relpath",
                            "ntpath.join", "PureWindowsPath", "pathlib.PureWindowsPath") \
                        and not allowed(path, node):
                    found.append("%s %s" % (where(path, node), text))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id in ("str", "os.fspath") and node.args:
                    inner = node.args[0]
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                            and inner.func.attr in ("relative_to", "resolve", "absolute"):
                        found.append("%s str(...%s()) gives backslashes on Windows; use "
                                     ".as_posix()" % (where(path, node), inner.func.attr))
                if isinstance(node, ast.Call) and dotted(node.func) == "os.fspath" and \
                        node.args and isinstance(node.args[0], ast.Call) and \
                        isinstance(node.args[0].func, ast.Attribute) and \
                        node.args[0].func.attr == "relative_to":
                    found.append("%s os.fspath(...relative_to())" % where(path, node))
        self.assertEqual(found, [])


# ---------------------------------------------------------------------- PostgreSQL's programs


PIPE_HELPERS_ALLOWED = {"read_version",
                        "_pid_is_another_program"}


def enclosing_function(node):
    for parent in ancestors(node):
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent
    return None


def captures_output(call):
    if is_true(keyword(call, "capture_output")):
        return True
    for name in ("stdout", "stderr", "stdin"):
        value = keyword(call, name)
        if value is not None and dotted(value) in ("subprocess.PIPE",):
            return True
    return dotted(call.func) in ("subprocess.check_output", "subprocess.getoutput")


class TestPostgresPrograms(unittest.TestCase):

    def test_pipes_only_in_the_allowed_helpers_that_never_run_pg_ctl(self):
        path = ROOT / "pcldb" / "store" / "pgcluster.py"
        found = []
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Call) and (dotted(node.func) or "").startswith("subprocess.") \
                    and captures_output(node):
                function = enclosing_function(node)
                name = function.name if function is not None else "<module>"
                if name not in PIPE_HELPERS_ALLOWED or "pg_ctl" in ast.unparse(function):
                    found.append("%s in %s" % (where(path, node), name))
        self.assertEqual(found, [], "pg_ctl start must never get a pipe")

    def test_no_pipe_anywhere_near_pg_ctl_in_the_package(self):
        found = []
        for path in code_files():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Call) and captures_output(node):
                    function = enclosing_function(node)
                    if function is not None and "pg_ctl" in ast.unparse(function):
                        found.append(where(path, node))
        self.assertEqual(found, [])

    def test_a_recorded_start_has_no_pipe_and_its_own_process_group_on_windows(self):
        from pcldb.config import Settings
        from pcldb.store import pgcluster
        import tempfile
        with tempfile.TemporaryDirectory(prefix="pcldb portability ") as folder:
            settings = Settings.for_test(Path(folder), engine="postgresql")
            cluster = pgcluster.LocalCluster(settings, test_mode=True)
            fake_bin = pgcluster.PgBin(Path(folder) / "bin", "18.7", 18, 7, ".exe")
            calls = []

            def record(command, **options):
                calls.append((command, options))
                return subprocess.CompletedProcess(command, 0)
            fake_subprocess = mock.MagicMock(wraps=subprocess)
            fake_subprocess.run.side_effect = record
            fake_subprocess.DEVNULL = subprocess.DEVNULL
            fake_subprocess.PIPE = subprocess.PIPE
            fake_subprocess.STDOUT = subprocess.STDOUT
            fake_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            fake_subprocess.SubprocessError = subprocess.SubprocessError
            fake_subprocess.CREATE_NO_WINDOW = 0x08000000
            fake_subprocess.CREATE_NEW_PROCESS_GROUP = 0x00000200
            status = {"initialized": True, "running": False, "pid": None, "port": 1,
                      "version": "18.7", "data_dir": folder, "bin_dir": folder}
            with mock.patch.object(pgcluster, "subprocess", fake_subprocess), \
                    mock.patch.object(pgcluster, "current_system", return_value="windows"), \
                    mock.patch.object(type(cluster), "pg_bin", new_callable=mock.PropertyMock,
                                      return_value=fake_bin), \
                    mock.patch.object(cluster, "status", return_value=status), \
                    mock.patch.object(cluster, "_check_before_start"), \
                    mock.patch.object(cluster, "_write_conf"):
                self.assertTrue(cluster.start())
        starts = [(command, options) for command, options in calls if "start" in command]
        self.assertEqual(len(starts), 1, calls)
        command, options = starts[0]
        self.assertTrue(command[0].endswith("pg_ctl.exe"), command)
        self.assertIs(options.get("stdin"), subprocess.DEVNULL)
        self.assertNotIn(options.get("stdout"), (subprocess.PIPE, None))
        self.assertNotEqual(options.get("stderr"), subprocess.PIPE)
        self.assertFalse(options.get("capture_output"))
        self.assertEqual(options.get("creationflags", 0) & 0x08000200, 0x08000200)
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "120")
        environment = options.get("env") or {}
        self.assertFalse([name for name in ("PGUSER", "PGPASSWORD", "PGDATA", "PGHOST",
                                            "PGPORT") if name in environment])

    def test_program_names_are_never_written_without_the_exe_handling(self):
        programs = {"initdb", "pg_ctl", "postgres", "pg_isready", "psql", "pg_dump",
                    "pg_restore", "pg_config", "createdb"}
        found = []
        for path in code_files():
            for node in ast.walk(parse(path)):
                # Path / "pg_ctl"  or  ["pg_ctl", ...] handed to subprocess.
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and \
                        isinstance(node.right, ast.Constant) and node.right.value in programs:
                    found.append("%s / %r" % (where(path, node), node.right.value))
                if isinstance(node, ast.Call) and (dotted(node.func) or "").startswith(
                        "subprocess.") and node.args and isinstance(node.args[0], ast.List) \
                        and node.args[0].elts and isinstance(node.args[0].elts[0], ast.Constant) \
                        and node.args[0].elts[0].value in programs:
                    found.append("%s %r run by name" % (where(path, node),
                                                        node.args[0].elts[0].value))
        self.assertEqual(found, [], "use PgBin.program(name), which adds .exe on Windows")

    def test_program_adds_exe_on_windows(self):
        from pcldb.store import pgcluster
        windows = pgcluster.PgBin(Path("bin"), "17.12", 17, 12, ".exe")
        self.assertEqual(windows.program("pg_ctl").name, "pg_ctl.exe")
        posix = pgcluster.PgBin(Path("bin"), "18.7", 18, 7, "")
        self.assertEqual(posix.program("pg_ctl").name, "pg_ctl")


# ---------------------------------------------------------------------- console text


CONSOLE_CALLS = {"print", "_say", "say", "write", "info", "warning", "error", "exception",
                 "debug", "critical", "log", "note", "ok", "problem", "fail", "step", "add_argument",
                 "add_parser", "ArgumentParser", "exit"}
ERROR_CLASSES = {"PclError", "NotFound", "Forbidden", "Unauthorized", "Conflict", "TooLarge",
                 "RateLimited", "Unavailable", "PlanError", "SystemExit"}


class TestConsoleText(unittest.TestCase):

    def test_text_written_to_the_console_is_ascii(self):
        found = []
        for path in code_files():
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else None)
                if name not in CONSOLE_CALLS and name not in ERROR_CLASSES:
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and \
                            not inner.value.isascii():
                        found.append("%s %r" % (where(path, inner), inner.value[:60]))
        self.assertEqual(found, [], "Windows consoles: ASCII only")

    def test_the_package_sources_are_ascii(self):
        # Simplest proof that no message, help text or docstring can put a character on a
        # Windows console that its code page lacks.
        found = [path.relative_to(ROOT).as_posix() for path in code_files()
                 if not path.read_bytes().isascii()]
        self.assertEqual(found, [])


# ---------------------------------------------------------------------- file names


WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
WINDOWS_BAD_CHARACTERS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
# The checkout on a GitHub Windows runner is D:\a\<repo>\<repo>\ (about 30 characters), and
# Windows paths stop at 260 characters unless long paths are switched on.
MAX_RELATIVE_PATH = 150


class TestFileNames(unittest.TestCase):

    def test_no_two_names_differ_only_by_letter_case(self):
        seen = {}
        clashes = []
        for path in repository_files():
            relative = path.relative_to(ROOT).as_posix()
            key = relative.casefold()
            if key in seen:
                clashes.append((seen[key], relative))
            seen[key] = relative
        self.assertEqual(clashes, [])

    def test_every_name_is_valid_on_windows(self):
        bad = []
        for path in repository_files():
            relative = path.relative_to(ROOT)
            for part in relative.parts:
                if WINDOWS_RESERVED.match(part) or WINDOWS_BAD_CHARACTERS.search(part) \
                        or part.endswith((".", " ")) or not part.isascii():
                    bad.append(relative.as_posix())
                    break
            if len(relative.as_posix()) > MAX_RELATIVE_PATH:
                bad.append(relative.as_posix() + " (too long)")
        self.assertEqual(bad, [])

    def test_the_name_rules_themselves(self):
        for name in ("CON", "nul.txt", "Com1.json", "lpt9"):
            self.assertTrue(WINDOWS_RESERVED.match(name), name)
        for name in ("console.py", "com10", "null.txt", "auxiliary.md"):
            self.assertFalse(WINDOWS_RESERVED.match(name), name)
        for name in ("a:b", "what?", 'say"hi"', "x|y", "tab\there"):
            self.assertTrue(WINDOWS_BAD_CHARACTERS.search(name), name)


# ---------------------------------------------------------------------- line endings


def is_text(data):
    if b"\x00" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


class TestLineEndings(unittest.TestCase):
    """.gitattributes says `* -text`: git converts nothing, so the bytes here are the bytes
    on every checkout (also on Windows, where core.autocrlf is often true)."""

    def test_gitattributes_turns_conversion_off_for_everything(self):
        lines = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        rules = [line.split() for line in lines if line.strip() and not line.startswith("#")]
        self.assertEqual(rules, [["*", "-text"]])

    def test_pcldb_cmd_has_crlf_on_every_line(self):
        data = (ROOT / "pcldb.cmd").read_bytes()
        self.assertTrue(data.endswith(b"\r\n"))
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "a line ends with LF only")
        self.assertEqual(data.count(b"\r"), data.count(b"\r\n"), "a lone CR")

    def test_every_other_text_file_has_lf_only(self):
        with_cr = []
        texts = 0
        for path in repository_files():
            if path.name == "pcldb.cmd":
                continue
            data = path.read_bytes()
            if not is_text(data):
                continue
            texts += 1
            if b"\r" in data:
                with_cr.append(path.relative_to(ROOT).as_posix())
        self.assertGreater(texts, 50)
        self.assertEqual(with_cr, [])

    def test_the_posix_launcher_is_lf_and_starts_with_its_shebang(self):
        data = (ROOT / "pcldb.sh").read_bytes()
        self.assertTrue(data.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", data)


# ---------------------------------------------------------------------- folders for a cluster

# The files whose temporary folders may hold a PostgreSQL cluster, and the one function in each
# that may make such a folder.
CLUSTER_FOLDER_FILES = ("tests/support.py", "tests/test_end_to_end.py", "tools/smoke.py")
TEMP_FOLDER_CALLS = ("mkdtemp", "TemporaryDirectory")


def temp_folder_calls_outside_the_helper(path):
    """Where `path` calls tempfile.mkdtemp or TemporaryDirectory outside make_temp_folder."""
    found = []
    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in TEMP_FOLDER_CALLS:
            continue
        inside = [parent.name for parent in ancestors(node)
                  if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if "make_temp_folder" not in inside:
            found.append("%s %s" % (where(path, node), name))
    return found


class TestClusterFolders(unittest.TestCase):
    """Python 3.13 and newer make tempfile.mkdtemp's folder (mode 0o700) on Windows with rights
    for SYSTEM, Administrators and the owner only. PostgreSQL's programs run with reduced rights
    there and cannot open anything in such a folder (initdb: Permission denied). So a folder
    that may hold a cluster comes from make_temp_folder (os.mkdir without a mode on Windows)."""

    def test_folders_that_may_hold_a_cluster_come_from_make_temp_folder(self):
        found = []
        for name in CLUSTER_FOLDER_FILES:
            found += temp_folder_calls_outside_the_helper(ROOT / name)
        self.assertEqual(found, [], "use make_temp_folder (tests/support.py, tools/smoke.py)")
        for name in ("tests/support.py", "tools/smoke.py"):
            helpers = [node for node in ast.walk(parse(ROOT / name))
                       if isinstance(node, ast.FunctionDef) and node.name == "make_temp_folder"]
            self.assertEqual(len(helpers), 1, name)
            self.assertIn("os.mkdir(path)", ast.unparse(helpers[0]), name)

    def test_the_checker_itself(self):
        folder = ROOT / "tests"
        sample = folder / "_portability_sample_does_not_exist.py"
        code = ("import tempfile\n"
                "def make_temp_folder(prefix):\n"
                "    return tempfile.mkdtemp(prefix=prefix)\n"
                "def other():\n"
                "    tempfile.mkdtemp()\n"
                "    with tempfile.TemporaryDirectory() as folder:\n"
                "        pass\n")
        with mock.patch.object(Path, "read_text", return_value=code):
            found = temp_folder_calls_outside_the_helper(sample)
        _parsed.pop(sample, None)
        self.assertEqual(sorted(item.split(" ", 1)[1] for item in found),
                         ["TemporaryDirectory", "mkdtemp"])


# ---------------------------------------------------------------------- Python 3.11


NEWER_THAN_3_11 = {
    # name: the version that added it (attribute or keyword uses outside a version guard)
    "itertools.batched": "3.12", "os.path.isjunction": "3.12", "os.path.splitroot": "3.12",
    "typing.override": "3.12", "logging.getHandlerByName": "3.12",
    "datetime.datetime.UTC": "3.11",
}
NEWER_KEYWORDS = {("rmtree", "onexc"): "3.12", ("sqlite3.connect", "autocommit"): "3.12",
                  ("TemporaryDirectory", "delete"): "3.12",
                  ("NamedTemporaryFile", "delete_on_close"): "3.12",
                  ("relative_to", "walk_up"): "3.12", ("glob", "case_sensitive"): "3.12",
                  ("add_argument", "deprecated"): "3.13"}


def under_version_guard(node):
    for parent in ancestors(node):
        if isinstance(parent, (ast.If, ast.IfExp)) and "version_info" in ast.unparse(parent.test):
            return True
    return False


class TestPython311(unittest.TestCase):
    """CI runs Python 3.11 on Windows; nothing here has a 3.11 to run on."""

    def test_every_file_parses_as_python_3_11(self):
        bad = []
        for path in python_files():
            try:
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))
            except SyntaxError as error:
                bad.append("%s:%s %s" % (path.relative_to(ROOT).as_posix(), error.lineno,
                                         error.msg))
        self.assertEqual(bad, [])

    def test_no_newer_library_feature_without_a_version_guard(self):
        found = []
        for path in python_files():
            if path == THIS_FILE:
                continue
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Attribute) and dotted(node) in NEWER_THAN_3_11 \
                        and NEWER_THAN_3_11[dotted(node)] > "3.11" \
                        and not under_version_guard(node) and not is_guarded(node):
                    found.append("%s %s" % (where(path, node), dotted(node)))
                if isinstance(node, ast.Call):
                    func = node.func
                    short = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    for name in {short, dotted(func)}:
                        for item in node.keywords:
                            if (name, item.arg) in NEWER_KEYWORDS and not under_version_guard(node):
                                found.append("%s %s(%s=) needs Python %s" % (
                                    where(path, node), name, item.arg,
                                    NEWER_KEYWORDS[(name, item.arg)]))
        self.assertEqual(found, [], "guard it with sys.version_info or use getattr")


if __name__ == "__main__":
    unittest.main()
