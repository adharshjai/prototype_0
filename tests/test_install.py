"""install.py: its decisions, without making a real environment (specification section 13)."""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path

from tests import support

PROJECT = Path(__file__).resolve().parent.parent
INSTALL = PROJECT / "install.py"


def load_install():
    spec = importlib.util.spec_from_file_location("pcldb_install_under_test", INSTALL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load_install()


class Recorder:
    """Stands for subprocess: remembers the commands and answers with fixed exit codes."""

    def __init__(self, codes=()):
        self.commands = []
        self.codes = list(codes)

    def __call__(self, command):
        self.commands.append(list(command))
        return self.codes.pop(0) if self.codes else 0


class InstallCase(unittest.TestCase):

    def setUp(self):
        self.temp = support.TempProject("sqlite", prefix="pcldb install ")
        self.addCleanup(self.temp.cleanup)
        self.project = str(self.temp.path)
        for name in ("requirements.txt", "requirements-globus.txt"):
            (self.temp.path / name).write_text("# test\n", encoding="utf-8")

    def run_install(self, argv=(), codes=(), **keywords):
        recorder = Recorder(codes)
        out = io.StringIO()
        keywords.setdefault("project", self.project)
        keywords.setdefault("python", "/somewhere/python3")
        keywords.setdefault("platform", "darwin")
        keywords.setdefault("version_info", (3, 13, 5))
        code = install.main(list(argv), run=recorder, out=out, **keywords)
        return code, out.getvalue(), recorder.commands

    def make_venv(self, system="posix"):
        path = Path(install.venv_python(self.project, system))
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        return path

    # -- the Python version ----------------------------------------------------------------------

    def test_an_old_python_is_refused_with_the_version_found(self):
        for version in ((3, 10, 12), (3, 9, 6), (3, 6, 0), (2, 7, 18)):
            with self.subTest(version=version):
                code, out, commands = self.run_install(version_info=version)
                self.assertEqual(code, 1)
                self.assertEqual(commands, [])
                self.assertIn("This is Python %d.%d.%d." % version, out)
                self.assertIn("Python 3.11 or newer", out)
                self.assertEqual(len(out.strip().splitlines()), 1)
                self.assertFalse((self.temp.path / ".venv").exists())

    def test_supported_pythons_are_accepted(self):
        for version in ((3, 11, 0), (3, 12, 7), (3, 13, 5), (3, 14, 0), (4, 0, 0)):
            self.assertIsNone(install.version_problem(version), version)
        self.assertIsNone(install.version_problem())      # the Python that runs the tests

    def test_a_named_tuple_version_works_too(self):
        self.assertIsNone(install.version_problem(sys.version_info))
        self.assertEqual(install.version_text(sys.version_info),
                         "%d.%d.%d" % sys.version_info[:3])

    def test_the_file_can_be_read_by_an_old_python(self):
        source = INSTALL.read_text(encoding="utf-8")
        self.assertTrue(source.isascii())
        ast.parse(source, feature_version=(3, 7))       # no syntax newer than Python 3.7
        self.assertNotIn("from __future__ import annotations", source)

    def test_only_the_standard_library_is_imported(self):
        tree = ast.parse(INSTALL.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertEqual(names, {"argparse", "os", "subprocess", "sys"})

    # -- the steps -------------------------------------------------------------------------------

    def test_a_fresh_folder_on_macos_or_linux(self):
        code, out, commands = self.run_install()
        self.assertEqual(code, 0, out)
        venv = os.path.join(self.project, ".venv")
        venv_python = os.path.join(venv, "bin", "python")
        self.assertEqual(commands, [
            ["/somewhere/python3", "-m", "venv", venv],
            [venv_python, "-m", "pip", "install", "--disable-pip-version-check",
             "-r", os.path.join(self.project, "requirements.txt")],
        ])
        lines = out.splitlines()
        self.assertIn("  ./pcldb.sh setup", lines)
        self.assertIn("  ./pcldb.sh serve --open", lines)
        self.assertLess(lines.index("  ./pcldb.sh setup"), lines.index("  ./pcldb.sh serve --open"))
        self.assertIn(self.project, out)
        self.assertTrue(out.isascii())

    def test_a_fresh_folder_on_windows(self):
        code, out, commands = self.run_install(platform="win32", python="C:\\Py\\python.exe")
        self.assertEqual(code, 0, out)
        venv_python = os.path.join(self.project, ".venv", "Scripts", "python.exe")
        self.assertEqual(commands[0][:3], ["C:\\Py\\python.exe", "-m", "venv"])
        self.assertEqual(commands[1][0], venv_python)
        lines = out.splitlines()
        self.assertIn("  .\\pcldb.cmd setup", lines)
        self.assertIn("  .\\pcldb.cmd serve --open", lines)
        self.assertNotIn("pcldb.sh", out)

    def test_globus_adds_the_second_requirements_file(self):
        code, out, commands = self.run_install(["--globus"])
        self.assertEqual(code, 0, out)
        self.assertEqual(commands[-1][-4:], [
            "-r", os.path.join(self.project, "requirements.txt"),
            "-r", os.path.join(self.project, "requirements-globus.txt")])
        self.assertIn("globus-sdk", out)
        _code, out, commands = self.run_install()
        self.assertNotIn("requirements-globus.txt", " ".join(commands[-1]))
        self.assertIn("install.py --globus", out)

    def test_an_environment_that_is_there_is_kept(self):
        self.make_venv()
        code, out, commands = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][1:4], ["-m", "pip", "install"])
        self.assertIn("is kept", out)

    def test_the_windows_environment_is_looked_for_in_scripts(self):
        self.make_venv("windows")
        _code, _out, commands = self.run_install(platform="win32")
        self.assertEqual(len(commands), 1)
        _code, _out, commands = self.run_install(platform="linux")
        self.assertEqual(len(commands), 2)      # a Windows environment is no use on Linux

    def test_a_failed_venv_stops_with_a_sentence(self):
        code, out, commands = self.run_install(codes=[1])
        self.assertEqual(code, 1)
        self.assertEqual(len(commands), 1)
        self.assertIn("could not be made", out)
        self.assertNotIn("pcldb.sh setup", out)

    def test_a_failed_pip_stops_with_a_sentence(self):
        code, out, commands = self.run_install(codes=[0, 1])
        self.assertEqual(code, 1)
        self.assertEqual(len(commands), 2)
        self.assertIn("could not be installed", out)
        self.assertIn("internet", out)
        self.assertNotIn("pcldb.sh setup", out)

    def test_a_program_that_cannot_be_started_stops_with_a_sentence(self):
        code, out, _commands = self.run_install(codes=[None])
        self.assertEqual(code, 1)
        self.assertIn("could not be made", out)

    def test_missing_requirements_are_named(self):
        (self.temp.path / "requirements-globus.txt").unlink()
        code, out, commands = self.run_install(["--globus"])
        self.assertEqual((code, commands), (1, []))
        self.assertIn("requirements-globus.txt", out)
        code, _out, _commands = self.run_install()
        self.assertEqual(code, 0)

    def test_help_and_wrong_options(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            code, _out, commands = self.run_install(["--help"])
        self.assertEqual((code, commands), (0, []))
        self.assertIn("--globus", shown.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            code, _out, commands = self.run_install(["--nonsense"])
        self.assertEqual((code, commands), (2, []))

    def test_a_path_with_a_space_stays_one_argument(self):
        self.assertIn(" ", self.project)
        _code, _out, commands = self.run_install(["--globus"])
        for command in commands:
            self.assertIsInstance(command, list)
            for part in command:
                self.assertIsInstance(part, str)
        self.assertIn(os.path.join(self.project, ".venv"), commands[0])

    # -- the small decisions ---------------------------------------------------------------------

    def test_system_name(self):
        self.assertEqual(install.system_name("win32"), "windows")
        self.assertEqual(install.system_name("darwin"), "posix")
        self.assertEqual(install.system_name("linux"), "posix")
        self.assertIn(install.system_name(), ("windows", "posix"))

    def test_next_commands_are_two_and_name_a_launcher_that_exists(self):
        for system, launcher in (("posix", "pcldb.sh"), ("windows", "pcldb.cmd")):
            commands = install.next_commands(system)
            self.assertEqual(len(commands), 2)
            self.assertTrue(all(launcher in command for command in commands))
            self.assertTrue((PROJECT / launcher).is_file())
            self.assertTrue(commands[0].endswith(" setup"))

    def test_the_real_project_folder_and_its_requirement_files(self):
        self.assertEqual(Path(install.project_dir()), PROJECT)
        for path in install.requirement_files(str(PROJECT), True):
            self.assertTrue(Path(path).is_file(), path)

    def test_the_default_runner_gives_none_for_a_program_that_is_not_there(self):
        self.assertIsNone(install._run([os.path.join(self.project, "no such program")]))


if __name__ == "__main__":
    unittest.main()
