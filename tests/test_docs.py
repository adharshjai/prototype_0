"""The documents (README.md, docs/*.md) against the code.

- Every pcldb command line in them (in shell, PowerShell and cmd code blocks, and in inline
  code) parses with the real argument parser of `pcldb/cli.py`: the command and its options
  exist.
- Every relative link points to a file that exists.
- No document holds anything that looks like a token or a key, and no UUID other than the two
  public ones (the ARC Projects collection and the Globus SDK tutorial client).

Nothing here starts a program, reads a settings file or uses the network.
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex
import unittest
from pathlib import Path

from pcldb import cli

PROJECT = Path(__file__).resolve().parent.parent

ALLOWED_UUIDS = {
    "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",     # ARC's public Projects collection
    "61338d24-54d5-408f-a10d-66c06b59f6d2",     # the Globus SDK tutorial client (public)
}

# Code blocks whose lines are commands to type.
COMMAND_LANGUAGES = {"sh", "shell", "bash", "console", "powershell", "ps1", "pwsh", "bat", "cmd"}
WINDOWS_LANGUAGES = {"powershell", "ps1", "pwsh", "bat", "cmd"}

# The ways the documents start pcldb. Group "rest" is what follows: the arguments.
_LAUNCHER = re.compile(
    r"^(?:\./pcldb\.sh|\.\\pcldb\.cmd|pcldb\.cmd|pcldb"
    r"|(?:python3?|py\s+-3|\.venv/bin/python|\.venv\\Scripts\\python\.exe)(?:\s+-B)?\s+-m\s+pcldb)"
    r"(?:\s+(?P<rest>.*))?$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
_PROMPT = re.compile(r"^(?:\$|PS>|PS [^>]*>|>)\s+")
_COMMENT = re.compile(r"\s+#\s.*$")
_FENCE = re.compile(r"^\s*```\s*([A-Za-z0-9_+-]*)\s*$")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

_TOKEN_LIKE = re.compile(r"pcl_[A-Za-z0-9_-]{20,}")
_KEY_LIKE = re.compile(r"(?<![A-Za-z0-9])sk-[0-9A-Fa-f]{8,}")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def documents():
    """The documents a person reads: README.md and every Markdown file in docs/."""
    found = [PROJECT / "README.md"]
    found += sorted((PROJECT / "docs").glob("*.md"))
    return found


def split_markdown(text):
    """([(language, line)] of fenced code, [paragraph text outside code fences])."""
    code, paragraphs, current = [], [], []
    language = None
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence:
            if language is None:
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
                language = fence.group(1).lower() or "text"
            else:
                language = None
            continue
        if language is not None:
            code.append((language, line))
        elif line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return code, paragraphs


def command_arguments(text, windows=False):
    """The arguments after the launcher when `text` is a pcldb command line, else None."""
    text = _PROMPT.sub("", text.strip())
    while _ASSIGNMENT.match(text):
        text = _ASSIGNMENT.sub("", text, count=1)
    if not windows:
        text = _COMMENT.sub("", text)
    match = _LAUNCHER.match(text)
    if not match:
        return None
    rest = (match.group("rest") or "").strip()
    if not rest:
        return None         # the launcher's name alone (`pcldb.cmd`) names the file, not a command
    windows = windows or text.startswith((".\\", "pcldb.cmd", ".venv\\"))
    return shlex.split(rest, posix=not windows)


def command_lines(path):
    """[(where, argument list)] of every pcldb command line in one document."""
    code, paragraphs = split_markdown(path.read_text(encoding="utf-8"))
    found = []
    for language, line in code:
        if language not in COMMAND_LANGUAGES:
            continue
        arguments = command_arguments(line, windows=language in WINDOWS_LANGUAGES)
        if arguments is not None:
            found.append(("%s, code: %s" % (path.name, line.strip()), arguments))
    for paragraph in paragraphs:
        for span in _INLINE_CODE.findall(paragraph):
            arguments = command_arguments(span)
            if arguments is not None:
                found.append(("%s, inline: %s" % (path.name, span), arguments))
    return found


def parses(arguments):
    """None when the real parser accepts the arguments, else what it said."""
    parser = cli.build_parser()
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser.parse_args(arguments)
    except SystemExit as stop:
        if stop.code in (0, None):      # --help, --version
            return None
        return err.getvalue().strip() or "exit %s" % stop.code
    if not getattr(args, "command", None):
        return "no command"
    return None


class TestDocumentsExist(unittest.TestCase):

    def test_the_documents_are_there(self):
        for name in ("README.md", "docs/API.md", "docs/SECURITY.md", "docs/GLOBUS_SEARCH.md",
                     "docs/WINDOWS.md"):
            self.assertTrue((PROJECT / name).is_file(), name)


class TestCommandLines(unittest.TestCase):

    def test_every_pcldb_command_line_parses(self):
        total = 0
        for path in documents():
            for where, arguments in command_lines(path):
                total += 1
                with self.subTest(where=where):
                    if len(arguments) == 1 and arguments[0] in cli.COMMANDS:
                        continue    # a mention of the command by name (`pcldb sql`): it exists
                    self.assertIsNone(parses(arguments))
        # The documents are full of commands; finding only a few means the reader broke.
        self.assertGreater(total, 60)

    def test_every_command_of_the_parser_is_documented(self):
        used = set()
        for path in documents():
            for _where, arguments in command_lines(path):
                words = [word for word in arguments if not word.startswith("-")]
                if words:
                    used.add(words[0])
        self.assertEqual(set(cli.COMMANDS) - used, set())

    def test_the_reader_finds_commands_in_each_form(self):
        self.assertEqual(command_arguments("./pcldb.sh setup --engine sqlite --demo"),
                         ["setup", "--engine", "sqlite", "--demo"])
        self.assertEqual(command_arguments(".\\pcldb.cmd register --tree C:\\data\\x"),
                         ["register", "--tree", "C:\\data\\x"])
        self.assertEqual(command_arguments("PCLDB_X=1 pcldb db status   # a note"),
                         ["db", "status"])
        self.assertEqual(command_arguments(".venv\\Scripts\\python.exe -m pcldb serve --open"),
                         ["serve", "--open"])
        self.assertEqual(command_arguments("py -3 -m pcldb doctor"), ["doctor"])
        self.assertIsNone(command_arguments("pcldb_config.json"))
        self.assertIsNone(command_arguments("python3 install.py --globus"))

    def test_a_wrong_command_is_caught(self):
        self.assertIsNotNone(parses(["setup", "--no-such-option"]))
        self.assertIsNotNone(parses(["no-such-command"]))
        self.assertIsNotNone(parses(["db", "explode"]))
        self.assertIsNone(parses(["search", "use-globus"]))


class TestLinks(unittest.TestCase):

    def test_every_relative_link_points_to_a_file(self):
        checked = 0
        for path in documents():
            text = path.read_text(encoding="utf-8")
            for target in _LINK.findall(text):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                checked += 1
                file_part = target.split("#", 1)[0]
                with self.subTest(document=path.name, link=target):
                    self.assertTrue((path.parent / file_part).exists())
        self.assertGreater(checked, 5)


class TestNothingSecret(unittest.TestCase):

    def files(self):
        return documents() + [PROJECT / "pcldb_config.example.json"]

    def test_no_token_or_key_like_text(self):
        for path in self.files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(document=path.name):
                self.assertEqual(_TOKEN_LIKE.findall(text), [])
                self.assertEqual(_KEY_LIKE.findall(text), [])

    def test_no_uuid_but_the_two_public_ones(self):
        for path in self.files():
            found = {value.lower() for value in _UUID.findall(path.read_text(encoding="utf-8"))}
            with self.subTest(document=path.name):
                self.assertEqual(found - ALLOWED_UUIDS, set())

    def test_no_message_tells_people_to_type_python_install_py(self):
        # NEW-11: a stock Mac has no "python" (only python3); Windows has "py -3".
        for path in sorted((PROJECT / "pcldb").rglob("*.py")):
            with self.subTest(file=path.name):
                self.assertNotIn("python install.py", path.read_text(encoding="utf-8"))

    def test_the_files_the_documents_tell_people_to_write_are_git_ignored(self):
        # NEW-10: a dump holds user names, token hashes and the logs; it must not be committed
        # when it is written into the clone as the README shows.
        import fnmatch
        import re
        patterns = [line.strip() for line in
                    (PROJECT / ".gitignore").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")]
        written = set()
        for path in documents():
            text = path.read_text(encoding="utf-8")
            written.update(re.findall(r"pcldb(?:\.sh|\.cmd)? (?:dump|export-sqlite) "
                                      r"([\w.-]+)", text))
        self.assertIn("pcl_dump.jsonl", written)
        for name in sorted(written):
            with self.subTest(file=name):
                self.assertTrue(any(fnmatch.fnmatch(name, pattern) for pattern in patterns),
                                name)

    def test_the_patterns_catch_what_they_should(self):
        self.assertTrue(_TOKEN_LIKE.search("Bearer pcl_" + "Ab3_-" * 9))
        self.assertTrue(_KEY_LIKE.search("key sk-0123456789abcdef"))
        self.assertFalse(_KEY_LIKE.search("a risk-free step"))
        self.assertTrue(_UUID.search("urn:pcl:dataset:80cb8636-d92d-5388-a71d-530a41d148ad"))
        self.assertFalse(_TOKEN_LIKE.search("pcl_catalog_copy.sqlite and pcl_session.json"))


if __name__ == "__main__":
    unittest.main()
