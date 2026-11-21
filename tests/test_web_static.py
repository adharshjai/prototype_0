"""Static checks of the website (pcldb/web/): section 12 of the specification.

The page shows text typed by other people and by an LLM, so most of these tests guard the
safety rules: no inline script or style, no markup built from strings, one audited outside
link, the token only in the Authorization header, the ARC user name only in this browser, and
only the backend paths of section 11. They also pin the two modes of section 12 (Normal: a
question and Globus; Expert: SQL and SSH) and the words a person reads.

With Node installed, three more things run (without it they are skipped with a reason and
everything else still runs): a syntax check, the pure functions of app.js
(tests/web_pure_test.js), the page itself on a small stand-in for a browser with a made-up
backend (tests/web_page_test.js), and the page on that stand-in with replies of the REAL server
(TestPageOnTheRealBackend: pcldb's server on demo data, every endpoint the page uses, on SQLite
and on PostgreSQL, then tests/web_replay_test.js on what it answered).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from tests import support

TESTS = Path(__file__).resolve().parent
WEB = TESTS.parent / "pcldb" / "web"
NODE_PURE_TESTS = TESTS / "web_pure_test.js"
NODE_PAGE_TESTS = TESTS / "web_page_test.js"
NODE_REPLAY_TESTS = TESTS / "web_replay_test.js"

STATIC_FILES = ("index.html", "app.js", "app.css", "favicon.svg")
VIEWS = ("normal", "expert", "status")

# Section 11: every path of the HTTP API. The website may call these and nothing else.
API_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/signin",
        "/api/v1/me",
        "/api/v1/status",
        "/api/v1/ask",
        "/api/v1/search",
        "/api/v1/session",
        "/api/v1/sql",
        "/api/v1/sql/schema",
        "/api/v1/sql/examples",
        "/api/v1/sql/history",
        "/api/v1/registrations",
    }
)

# Never in the script or the style sheet: ways to turn text into markup or code, to set a
# style attribute from script, to keep data anywhere but the two audited stores, or to reach an
# outside address.
FORBIDDEN_IN_CODE = (
    "innerHTML",
    "outerHTML",
    "insertAdjacent",
    "innerText",
    "outerText",
    "setHTML",
    "insertHTML",
    "document.write",
    "eval(",
    "new Function",
    "setAttribute('style'",
    'setAttribute("style"',
    ".style.",
    "javascript:",
    "http://",
    "cssText",
    "srcdoc",
    "createContextualFragment",
    "DOMParser",
    "document.cookie",
    "indexedDB",
    "serviceWorker",
    "postMessage",
    "window.name",
    "console.",
    "XMLHttpRequest",
    "WebSocket",
    "EventSource",
    "sendBeacon",
    "importScripts",
    "import(",
    "Worker(",
    "window.open",
    "location.href",
    "location.assign",
    "location.hash =",
    "debugger",
)

# What el() may make. Nothing here can load, run or link to anything.
SAFE_TAGS = frozenset(
    "button caption code dd details div dl dt h3 h4 li option p pre section span summary table "
    "tbody td th thead time tr ul wbr".split()
)
SAFE_ATTRIBUTES = frozenset(
    "class type title scope colspan datetime tabindex role hidden value id aria-label aria-live "
    "aria-expanded aria-controls".split()
)

# Each one finds sessions in the demo data with the keyword rules alone (no assistant):
# TestPageOnTheRealBackend asks every one of them and checks that.
EXAMPLE_QUESTIONS = (
    "Which sessions have adenine?",
    "Show Raman sessions from the last 7 days",
    "How many sessions per instrument?",
    "Which sessions still need review?",
    "Which sessions were partial or failed?",
)

# Words that are jargon to most lab members (ground rule 5). None may be in a sentence the page
# shows. "token" and "SQL" are the names of things people handle here, so they are fine.
JARGON = (
    "DSN", "outbox", "payload", "endpoint", "stack trace", "exception", "localStorage",
    "sessionStorage", "regex", "boolean", "JSON", "HTTP", "API", "fetch", "DOM", "CSP", "URL", "URI",
    "LLM", "stand-in", "GSearch", "gmeta", "facet", "bucket", "rollup", "upsert", "hash",
)


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


class PageParser(HTMLParser):
    """Collects what the checks need from index.html."""

    VOID = {"meta", "link", "input", "br", "hr", "img", "source", "wbr", "area", "base", "col", "embed", "track"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text: dict[int, str] = {}  # index in self.elements -> the text directly inside
        self.all_text: dict[int, str] = {}  # index -> all the text inside, nested elements included
        self.parent: dict[int, int | None] = {}
        self._open: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))
        index = len(self.elements) - 1
        self.parent[index] = self._open[-1] if self._open else None
        if tag not in self.VOID:
            self._open.append(index)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))
        self.parent[len(self.elements) - 1] = self._open[-1] if self._open else None

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self._open) - 1, -1, -1):
            if self.elements[self._open[position]][0] == tag:
                del self._open[position:]
                return

    def handle_data(self, data: str) -> None:
        if self._open:
            index = self._open[-1]
            self.text[index] = self.text.get(index, "") + data
            for opened in self._open:
                self.all_text[opened] = self.all_text.get(opened, "") + data

    def all(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.elements if name == tag]

    def texts(self, tag: str) -> list[str]:
        return [
            " ".join(self.text.get(index, "").split())
            for index, (name, _attrs) in enumerate(self.elements)
            if name == tag
        ]

    def index_of(self, element_id: str) -> int:
        return next(index for index, (_name, attrs) in enumerate(self.elements) if attrs.get("id") == element_id)

    def inner_text(self, element_id: str) -> str:
        """All the text inside the element with this id, as one line."""
        return " ".join(self.all_text.get(self.index_of(element_id), "").split())

    def inside(self, index: int, element_id: str) -> bool:
        """True when the element at index is the one with this id or sits inside it."""
        target = self.index_of(element_id)
        current: int | None = index
        while current is not None:
            if current == target:
                return True
            current = self.parent.get(current)
        return False

    def ids(self) -> list[str]:
        return [attrs["id"] for _name, attrs in self.elements if attrs.get("id")]

    def classes(self) -> set[str]:
        found: set[str] = set()
        for _name, attrs in self.elements:
            found.update((attrs.get("class") or "").split())
        return found

    def page_text(self) -> str:
        """Every word a person can read in the page as it is delivered (not script or style)."""
        parts = [text for index, text in self.text.items() if self.elements[index][0] not in ("script", "style")]
        for _name, attrs in self.elements:
            parts.extend(value for key, value in attrs.items() if key in ("title", "aria-label", "placeholder") and value)
        return " ".join(" ".join(parts).split())


def parse_page() -> PageParser:
    parser = PageParser()
    parser.feed(read("index.html"))
    parser.close()
    return parser


def strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def css_classes(css: str) -> set[str]:
    """Every class name used in a selector of the style sheet."""
    found: set[str] = set()
    for prelude in re.findall(r"([^{}]+)\{", strip_css_comments(css)):
        if prelude.strip().startswith("@"):
            continue
        found.update(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", prelude))
    return found


def js_code(js: str) -> str:
    """The script without its comment lines."""
    return "\n".join(line for line in js.splitlines() if not line.strip().startswith(("//", "*", "/*")))


def js_string_literals(js: str) -> list[str]:
    """Single-quoted and double-quoted string literals of the script (comment lines left out)."""
    code = js_code(js)
    found = re.findall(r"'((?:[^'\\\n]|\\.)*)'", code)
    found += re.findall(r'"((?:[^"\\\n]|\\.)*)"', code)
    return found


def js_sentences(js: str) -> list[str]:
    """String literals that are words for a person: they hold at least two spaces."""
    return [literal for literal in js_string_literals(js) if literal.count(" ") >= 2]


def js_classes(js: str) -> set[str]:
    """Class names the script names directly: {class: '...'}, className and classList calls."""
    found: set[str] = set()
    for literal in re.findall(r"\bclass: '([^']*)'", js):
        found.update(literal.split())
    for literal in re.findall(r"\.className = '([^']*)'", js):
        found.update(literal.split())
    for literal in re.findall(r"classList\.(?:add|remove|toggle)\('([^']*)'", js):
        found.update(literal.split())
    return found


def js_list(js: str, name: str) -> list[str]:
    """The strings of `const NAME = ['a', 'b', ...];` in the script."""
    block = re.search(r"const " + re.escape(name) + r" = \[(.*?)\];", js, flags=re.S)
    assert block, f"app.js has no list named {name}"
    return re.findall(r"'([^']*)'", block.group(1))


def js_function(js: str, name: str) -> str:
    """The text of one function of the script, from its first line to the closing brace."""
    start = re.search(r"^( *)(?:async )?function " + re.escape(name) + r"\(", js, flags=re.M)
    assert start, f"app.js has no function named {name}"
    end = js.index("\n" + start.group(1) + "}\n", start.start())
    return js[start.start() : end + len(start.group(1)) + 2]


def css_variables(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """The colour custom properties of :root: (light scheme, dark scheme)."""
    clean = strip_css_comments(css)
    light_block = re.search(r":root\s*\{([^}]*)\}", clean)
    dark_block = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}", clean)
    assert light_block and dark_block, "app.css must define :root and redefine it for the dark scheme"

    def colours(block: str) -> dict[str, str]:
        return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))

    light = colours(light_block.group(1))
    dark = dict(light)
    dark.update(colours(dark_block.group(1)))
    return light, dark


def luminance(colour: str) -> float:
    def channel(value: int) -> float:
        part = value / 255
        return part / 12.92 if part <= 0.04045 else ((part + 0.055) / 1.055) ** 2.4

    red, green, blue = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def darkened(colour: str, black: float) -> str:
    """The colour under a layer of black with this opacity (the hover shade of the main button)."""
    parts = [round(int(colour[i : i + 2], 16) * (1 - black)) for i in (1, 3, 5)]
    return "#" + "".join(f"{part:02x}" for part in parts)


# (text colour, background): every pair app.css puts on the page as readable text. 4.5 to 1.
TEXT_PAIRS = (
    ("text", "bg"), ("text", "surface"), ("text", "surface-2"),
    ("text", "accent-soft"), ("text", "warning-soft"), ("text", "danger-soft"), ("text", "ok-soft"),
    ("muted", "bg"), ("muted", "surface"), ("muted", "surface-2"), ("muted", "accent-soft"),
    ("accent", "bg"), ("accent", "surface"), ("accent", "surface-2"), ("accent", "accent-soft"),
    ("accent-text", "accent"),
    ("danger", "bg"), ("danger", "surface"), ("danger", "surface-2"), ("danger", "danger-soft"),
    ("warning", "bg"), ("warning", "surface"), ("warning", "warning-soft"),
    ("ok", "surface"), ("ok", "ok-soft"),
)
# The borders of fields and buttons, the focus ring, the marks of tags: 3 to 1.
EDGE_PAIRS = (
    ("border-strong", "bg"), ("border-strong", "surface"), ("border-strong", "surface-2"),
    ("accent", "bg"), ("accent", "surface"), ("accent", "surface-2"), ("accent", "accent-soft"),
    ("warning", "warning-soft"), ("danger", "danger-soft"), ("ok", "ok-soft"),
)


class TestFiles(unittest.TestCase):
    def test_the_four_files_exist_and_are_plain_text(self) -> None:
        for name in STATIC_FILES:
            with self.subTest(file=name):
                data = (WEB / name).read_bytes()
                self.assertGreater(len(data), 100)
                self.assertLess(len(data), 300_000, "the website has no build step: keep the files small")
                # ASCII only, so the page reads the same whatever charset a server names, and no
                # character that cannot be seen hides in the source.
                self.assertTrue(data.isascii(), "use \\u escapes instead of non-ASCII characters")
                self.assertNotIn(b"\r", data, "LF line ends only")
                self.assertTrue(data.endswith(b"\n"))
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "no byte order mark")

    def test_nothing_else_is_in_the_folder(self) -> None:
        # The server serves a fixed table of names, so a stray file here (a backup, a source
        # map, a second script) would be dead weight that looks like part of the site.
        ignored = {"__init__.py", "__pycache__"}
        names = sorted(path.name for path in WEB.iterdir()
                       if not path.name.startswith(".") and path.name not in ignored)
        self.assertEqual(names, sorted(STATIC_FILES))

    def test_the_test_scripts_are_plain_text_too(self) -> None:
        for path in (NODE_PURE_TESTS, NODE_PAGE_TESTS, NODE_REPLAY_TESTS, Path(__file__)):
            with self.subTest(file=path.name):
                self.assertTrue(path.read_bytes().isascii(), "an unseen character could hide in a test")


class TestIndexHtml(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = read("index.html")
        cls.page = parse_page()

    def test_the_query_box_comes_before_the_list_of_tables(self) -> None:
        # K-06: on a phone (one column) nobody should scroll past every table to reach the box.
        self.assertLess(self.html.index('id="sql-text"'), self.html.index('id="schema-list"'))
        css = read("app.css")
        wide = css[css.index("@media (min-width: 900px)"):]
        self.assertIn("grid-column: 1;", wide[:wide.index("}\n}")])

    def test_no_inline_script(self) -> None:
        scripts = self.page.all("script")
        self.assertEqual(len(scripts), 1, "one script element: app.js")
        self.assertEqual(scripts[0].get("src"), "app.js")
        self.assertIn("defer", scripts[0])
        self.assertEqual(self.page.texts("script"), [""], "the script element must be empty")

    def test_no_event_attributes_no_style_attributes_no_style_elements(self) -> None:
        self.assertEqual(self.page.all("style"), [])
        for tag, attrs in self.page.elements:
            for name in attrs:
                with self.subTest(tag=tag, attribute=name):
                    self.assertFalse(name.lower().startswith("on"), "no inline event handlers")
                    self.assertNotEqual(name.lower(), "style", "no style attributes")

    def test_no_outside_reference_at_all(self) -> None:
        lowered = self.html.lower()
        for needle in ("http://", "https://", "javascript:", "data:", "blob:", "//cdn", "@import"):
            self.assertFalse(needle in lowered, f"{needle!r} is in index.html")
        for tag, attrs in self.page.elements:
            for name in ("href", "src", "action", "formaction", "poster", "data", "srcset", "ping", "manifest"):
                value = attrs.get(name)
                if value is None:
                    continue
                with self.subTest(tag=tag, attribute=name, value=value):
                    if value.startswith("#"):
                        self.assertIn(value, tuple("#" + view for view in VIEWS))
                    else:
                        self.assertIn(value, STATIC_FILES, "only the site's own files, by plain name")
                        self.assertTrue((WEB / value).is_file())

    def test_no_element_that_loads_or_frames_other_content(self) -> None:
        for tag in ("base", "iframe", "frame", "object", "embed", "applet", "img", "video", "audio", "source",
                    "svg", "math", "template", "portal"):
            self.assertEqual(self.page.all(tag), [], tag)
        for meta in self.page.all("meta"):
            self.assertNotIn("http-equiv", meta)

    def test_head_names_the_three_files(self) -> None:
        links = self.page.all("link")
        self.assertEqual(sorted((link.get("rel"), link.get("href")) for link in links),
                         [("icon", "favicon.svg"), ("stylesheet", "app.css")])
        self.assertEqual(self.page.all("html")[0].get("lang"), "en")
        metas = self.page.all("meta")
        self.assertTrue(any(meta.get("charset", "").lower() == "utf-8" for meta in metas))
        self.assertTrue(any(meta.get("name") == "viewport" and "width=device-width" in (meta.get("content") or "")
                            for meta in metas))
        self.assertFalse(any("user-scalable" in (meta.get("content") or "") or "maximum-scale" in (meta.get("content") or "")
                             for meta in metas), "people must be able to zoom")
        self.assertTrue(any(meta.get("name") == "referrer" and meta.get("content") == "no-referrer"
                            for meta in metas))
        self.assertTrue(any(meta.get("name") == "color-scheme" and meta.get("content") == "light dark"
                            for meta in metas))
        self.assertEqual(self.page.texts("title"), ["PCL data"])
        self.assertEqual(self.page.texts("h1"), ["PCL data"])

    def test_forms_can_never_put_a_field_into_the_address(self) -> None:
        # If the script failed to load, a form sent the browser's own way must not turn the
        # token or a question into a query string: POST, no action, and no field has a name.
        forms = self.page.all("form")
        self.assertEqual(len(forms), 2)
        for form in forms:
            self.assertEqual((form.get("method") or "").lower(), "post")
            self.assertNotIn("action", form)
        for tag in ("input", "textarea", "select"):
            for attrs in self.page.all(tag):
                self.assertNotIn("name", attrs, f"{tag} #{attrs.get('id')} needs no name")
        # The ARC user name is not inside a form at all: nothing can ever submit it.
        user = self.page.index_of("arc-user")
        for form_id in ("signin-form", "ask-form"):
            self.assertFalse(self.page.inside(user, form_id))

    def test_the_token_field_hides_what_is_pasted(self) -> None:
        field = next(attrs for attrs in self.page.all("input") if attrs.get("id") == "signin-token")
        self.assertEqual(field.get("type"), "password")
        self.assertEqual(field.get("autocomplete"), "off")
        self.assertNotIn("value", field)

    def test_ids_are_unique_and_references_resolve(self) -> None:
        ids = self.page.ids()
        self.assertEqual(len(ids), len(set(ids)), "an id is used twice")
        known = set(ids)
        for tag, attrs in self.page.elements:
            for name in ("for", "aria-labelledby", "aria-describedby", "aria-controls"):
                for target in (attrs.get(name) or "").split():
                    with self.subTest(tag=tag, attribute=name, target=target):
                        self.assertIn(target, known)

    def test_every_field_has_a_label_and_every_button_a_type(self) -> None:
        labelled = {attrs.get("for") for attrs in self.page.all("label")}
        for tag in ("input", "textarea", "select"):
            for attrs in self.page.all(tag):
                with self.subTest(tag=tag, id=attrs.get("id")):
                    self.assertIn(attrs.get("id"), labelled)
                    self.assertNotIn("placeholder", attrs, "a placeholder is not a label and reads too pale")
        for attrs in self.page.all("button"):
            with self.subTest(button=attrs.get("id")):
                self.assertIn(attrs.get("type"), ("button", "submit"))
        for index, (tag, _attrs) in enumerate(self.page.elements):
            if tag == "button":
                self.assertTrue(" ".join(self.page.all_text.get(index, "").split()), "a button without words")

    def test_the_three_views_and_the_switch_between_them(self) -> None:
        # Section 12: #normal (default), #expert (only with the capability "sql"), #status, and
        # the switch says in words what each mode is.
        links = {attrs.get("id"): attrs.get("href") for attrs in self.page.all("a")}
        self.assertEqual(links, {"tab-normal": "#normal", "tab-expert": "#expert", "tab-status": "#status"})
        self.assertEqual(self.page.inner_text("tab-normal"), "Normal: ask in plain English, files through Globus")
        self.assertEqual(self.page.inner_text("tab-expert"), "Expert: SQL, files through SSH")
        self.assertEqual(self.page.inner_text("tab-status"), "Status")
        nav = next(attrs for attrs in self.page.all("nav") if attrs.get("id") == "tabs")
        self.assertTrue(nav.get("aria-label"))
        ids = set(self.page.ids())
        for view in VIEWS:
            self.assertIn(f"panel-{view}", ids)
            self.assertIn(f"{view}-title", ids)
        by_id = {attrs.get("id"): attrs for _tag, attrs in self.page.elements if attrs.get("id")}
        # Expert stays out of the page until the backend says the person may use it.
        self.assertIn("hidden", by_id["tab-expert"])
        self.assertIn("hidden", by_id["panel-expert"])
        self.assertIn("hidden", by_id["panel-status"])
        self.assertNotIn("hidden", by_id["tab-normal"])
        self.assertNotIn("hidden", by_id["panel-normal"])
        # Nothing of the app shows before someone is signed in.
        for name in ("view-app", "view-signin", "tabs", "who"):
            self.assertIn("hidden", by_id[name])
        # A view's heading can take the focus when the view opens.
        for view in VIEWS:
            self.assertEqual(by_id[f"{view}-title"].get("tabindex"), "-1")
        # No element carries a view's name as its id, so "#normal" never scrolls the page.
        self.assertFalse({"normal", "expert", "status", "ask", "sql", "signin"} & ids)

    def test_each_mode_says_how_its_files_are_reached(self) -> None:
        # Section 12: a short "Files in this mode" note in both modes.
        self.assertEqual(self.page.texts("h3").count("Files in this mode"), 2)
        normal = self.page.inner_text("panel-normal")
        expert = self.page.inner_text("panel-expert")
        self.assertIn("Files in this mode In Normal mode you reach a session's files, and add new ones, through Globus.",
                      normal)
        self.assertNotIn("SSH", normal)
        self.assertIn("Files in this mode In Expert mode you reach a session's files, and add new ones, over SSH.",
                      expert)
        self.assertNotIn("Globus", expert)
        self.assertIn("a column named path or session_path opens that session", expert)

    def test_the_arc_user_name_field(self) -> None:
        # Section 12: "Your ARC user name", kept only in this browser, never sent to the server.
        label = next(index for index, (tag, attrs) in enumerate(self.page.elements)
                     if tag == "label" and attrs.get("for") == "arc-user")
        self.assertEqual(" ".join(self.page.text[label].split()), "Your ARC user name")
        field = next(attrs for attrs in self.page.all("input") if attrs.get("id") == "arc-user")
        self.assertEqual(field.get("type"), "text")
        self.assertEqual(field.get("autocomplete"), "off")
        self.assertEqual(field.get("spellcheck"), "false")
        self.assertEqual(field.get("maxlength"), "64")
        self.assertNotIn("value", field)
        self.assertEqual(field.get("aria-describedby"), "arc-user-help arc-user-status")
        help_text = self.page.inner_text("arc-user-help")
        self.assertIn("kept only in this browser", help_text)
        self.assertIn("never sent to the server", help_text)
        self.assertIn("Empty the box to remove it", help_text)
        self.assertTrue(self.page.inside(self.page.index_of("arc-user"), "panel-expert"))

    def test_the_status_view_sums_up_the_two_modes(self) -> None:
        # Section 12: "a two-column summary of the two lanes" (the table of section 0).
        self.assertEqual(self.page.texts("h4"), ["Normal", "Expert"])
        rows = self.page.texts("dt")
        for name in ("Who", "Find data", "Get to the files", "Add files"):
            self.assertEqual(rows.count(name), 2, name)
        text = self.page.inner_text("panel-status")
        for needle in ("Two ways to work", "Every registered session can be found both ways",
                       "A question in plain English.", "Globus: the session's folder opens in the Globus web app.",
                       "Globus Transfer.", "People with the analyst or admin role.", "SQL that can only read",
                       "SSH: the path on ARC, and scp, sftp and rsync commands ready to copy.",
                       "An upload over SSH.", "Newest registrations"):
            self.assertIn(needle, text)

    def test_status_lines_are_announced_and_the_detail_is_a_dialog(self) -> None:
        by_id = {attrs.get("id"): attrs for _tag, attrs in self.page.elements if attrs.get("id")}
        for line in ("ask-status", "sql-status", "status-line", "boot-status", "signin-note", "schema-status",
                     "history-status", "arc-user-status"):
            with self.subTest(line=line):
                self.assertEqual(by_id[line].get("aria-live"), "polite")
                self.assertEqual(by_id[line].get("role"), "status")
        self.assertEqual(by_id["sql-error"].get("role"), "alert")
        self.assertEqual(by_id["ask-results"].get("aria-busy"), "false")
        dialogs = self.page.all("dialog")
        self.assertEqual(len(dialogs), 1)
        self.assertEqual(dialogs[0].get("id"), "detail")
        self.assertEqual(dialogs[0].get("aria-labelledby"), "detail-title")
        self.assertNotIn("open", dialogs[0])
        self.assertIn("detail-close", by_id)
        self.assertEqual(len(self.page.all("main")), 1)
        self.assertEqual(len(self.page.all("h1")), 1)

    def test_the_sql_editor(self) -> None:
        editor = next(attrs for attrs in self.page.all("textarea") if attrs.get("id") == "sql-text")
        self.assertEqual(editor.get("spellcheck"), "false")
        self.assertEqual(editor.get("aria-describedby"), "sql-hint")
        # The Tab key writes spaces in the box, so the hint must say how to leave it.
        self.assertIn("Press Escape to leave the box", self.page.inner_text("sql-hint"))

    def test_example_questions(self) -> None:
        buttons = self.page.texts("button")
        for question in EXAMPLE_QUESTIONS:
            self.assertIn(question, buttons)
        question = next(attrs for attrs in self.page.all("input") if attrs.get("id") == "ask-question")
        self.assertEqual(question.get("maxlength"), "500", "section 8: a question has 1 to 500 characters")

    def test_sign_in_help_says_where_the_token_is(self) -> None:
        text = " ".join(self.html.split())
        self.assertIn("Paste your access token", text)
        self.assertIn("var/admin_token.txt", text)
        # NEW-11: commands that work as typed (a bare "pcldb" is not a command)
        self.assertIn("<code>./pcldb.sh serve --open</code>", text)
        self.assertIn("<code>.\\pcldb.cmd serve --open</code>", text)

    def test_the_words_of_the_page_are_plain(self) -> None:
        text = self.page.page_text()
        for word in JARGON:
            with self.subTest(word=word):
                self.assertIsNone(re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", text), text)
        # The views are called Normal and Expert everywhere, never by the names of an early build.
        self.assertIsNone(re.search(r"\b(Ask|SQL|Status|Normal|Expert|this|the) tab\b", text))


class TestAppJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = read("app.js")
        cls.code = js_code(cls.js)

    def has(self, needle: str, times: int | None = None) -> None:
        if times is None:
            self.assertTrue(needle in self.code, f"{needle!r} is not in the code of app.js")
        else:
            self.assertEqual(self.code.count(needle), times, f"{needle!r} should be in app.js {times} time(s)")

    def lacks(self, needle: str) -> None:
        self.assertFalse(needle in self.code, f"{needle!r} is in the code of app.js")

    def test_strict_classic_script(self) -> None:
        self.assertTrue(self.js.startswith("'use strict';\n"))
        self.assertIsNone(re.search(r"^\s*(import|export)\s", self.js, flags=re.M), "a classic script")
        self.assertEqual(self.code.count("module.exports"), 2)
        self.has("if (typeof module !== 'undefined' && module.exports) {")
        self.has("if (typeof document !== 'undefined' && typeof window !== 'undefined') {")
        # Everything lives inside one function, so the page gets no new global name.
        top_level = [line for line in self.code.splitlines() if line and not line[0].isspace()]
        self.assertEqual(top_level, ["'use strict';", "(function () {", "})();"])
        self.assertIsNone(re.search(r"\b(window|globalThis|self)\.\w+\s*=[^=]", self.code))

    def test_forbidden_strings(self) -> None:
        for name in ("app.js", "app.css"):
            text = read(name)
            for needle in FORBIDDEN_IN_CODE:
                with self.subTest(file=name, needle=needle):
                    self.assertFalse(needle in text, f"{needle!r} is in {name}")
        # Timers never get code as text.
        self.assertIsNone(re.search(r"set(Timeout|Interval)\(\s*['\"`]", self.js))

    def test_the_one_https_string_is_the_comment_at_the_link_check(self) -> None:
        lines = [line for line in self.js.splitlines() if "https://" in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].strip().startswith("//"), "only in a comment")
        self.assertIn("https://app.globus.org/", lines[0])
        self.assertFalse("https://" in read("app.css"), "an https address is in app.css")
        # The check itself: visible ASCII only, then the parsed protocol, host and port, no
        # credentials, and the text as it came must start with the same origin.
        check = js_function(self.js, "safeGlobusUrl")
        for needle in ("if (!/^[!-\\[\\]-~]+$/.test(value)) return null;",
                       "url = new URL(value);",
                       "if (url.protocol !== 'https:') return null;",
                       "if (url.host !== 'app.globus.org') return null;",
                       "if (url.hostname !== 'app.globus.org' || url.port !== '') return null;",
                       "if (url.username !== '' || url.password !== '') return null;",
                       "if (value.slice(0, start.length) !== start) return null;",
                       "return url.href;"):
            self.assertIn(needle, check)
        self.assertNotIn("new URL(value,", check, "never with a base address: a relative value must fail")

    def test_every_api_path_is_in_section_11(self) -> None:
        found = set(re.findall(r"""['"`](/api/[^'"`]*)['"`]""", self.js))
        self.assertGreaterEqual(len(found), 9)
        for item in found:
            with self.subTest(path=item):
                self.assertIn(item, API_PATHS)
        # No path is built from pieces, and nothing but the table reaches fetch.
        self.assertEqual(set(re.findall(r"/api/[\w/]*", self.js)), found)
        self.assertEqual(self.code.count("fetch("), 1, "one place talks to the backend")
        self.has("response = await fetch(path, request);")
        self.has("if (!isApiPath(path)) throw")

    def test_every_request_the_page_makes(self) -> None:
        # Method, address and body of each call: the request shapes of section 11 and nothing
        # more. A new call must be added here on purpose.
        calls = sorted(set(re.findall(r"api\('(GET|POST)', ([\w.]+(?:\(\w+\))?)(?:, (\{[^}]*\}|\w+))?", self.code)))
        self.assertEqual(calls, sorted([
            ("POST", "API.signin", "{code: code}"),
            ("GET", "API.me", ""),
            ("GET", "API.me", "undefined"),
            ("GET", "API.status", "undefined"),
            ("POST", "API.ask", "{question: question, include_simulated: includeSimulated}"),
            ("GET", "sessionAddress(path)", "undefined"),
            ("POST", "API.sql", "request"),
            ("GET", "API.sqlSchema", "undefined"),
            ("GET", "API.sqlExamples", "undefined"),
            ("GET", "API.sqlHistory", "undefined"),
        ]))
        self.assertEqual(self.code.count("api('"), 10)
        run_sql = js_function(self.js, "runSql")
        self.assertIn("const request = {sql: sql};", run_sql)
        self.assertIn("request.max_rows = Math.floor(maxRows);", run_sql)
        self.assertEqual(run_sql.count("request."), 1, "sql and max_rows are all that is sent")
        self.has("return API.session + '?path=' + encodeURIComponent(textOf(path));")

    def test_requests_stay_on_this_site(self) -> None:
        for option in ("mode: 'same-origin'", "credentials: 'omit'", "redirect: 'error'",
                       "referrerPolicy: 'no-referrer'", "cache: 'no-store'"):
            self.has(option)
        self.has("headers['Content-Type'] = 'application/json';")

    def test_a_request_never_waits_for_ever(self) -> None:
        api = js_function(self.js, "api")
        for needle in ("const control = new AbortController();", "signal: control.signal,",
                       "}, o.waitMs || WAIT_MS.other);", "tooLate = true;",
                       "throw failed(tooLate ? 'timeout' : 'cancelled', 0, null);",
                       "window.clearTimeout(timer);", "o.signal.removeEventListener('abort', onCancel);"):
            self.assertIn(needle, api)
        self.has("const WAIT_MS = {ask: 300000, sql: 120000, other: 30000};")
        self.has("{signal: run.signal, waitMs: WAIT_MS.ask}")
        self.has("{signal: run.signal, waitMs: WAIT_MS.sql}")

    def test_the_token_is_only_in_session_storage_and_the_authorization_header(self) -> None:
        self.assertEqual(self.code.count("'pcldb.token'"), 1)
        self.assertEqual(self.code.count("window.sessionStorage."), 3)  # get, set, remove
        self.assertEqual(self.code.count("sessionStorage"), 3)
        for needle in ("window.sessionStorage.getItem(TOKEN_KEY)", "window.sessionStorage.setItem(TOKEN_KEY, token)",
                       "window.sessionStorage.removeItem(TOKEN_KEY)"):
            self.has(needle, 1)
        self.assertEqual(self.code.count("TOKEN_KEY"), 4)   # the name and its three uses
        self.assertEqual(self.code.count("Authorization"), 1)
        self.has("headers['Authorization'] = 'Bearer ' + token;")
        # It is forgotten on sign out and when the backend answers 401.
        self.assertIn("forgetToken();", js_function(self.js, "signOut"))
        self.assertIn("if (response.status === 401 && !o.anonymous && !o.token) {", js_function(self.js, "api"))
        self.assertIn("if (token === state.token) signOut(", js_function(self.js, "api"))
        self.assertIn("field.value = '';", js_function(self.js, "onSigninSubmit"))
        # No message, title, address or element is ever built from the token.
        for line in self.code.splitlines():
            if re.search(r"\b(token|candidate|stored)\b", line) and re.search(
                r"textContent|setText|setLine|setAttribute|title|createTextNode|new Error|location|replaceState|"
                r"\bel\(|append\(|localStorage|encodeURIComponent", line
            ):
                self.fail("the token must not reach the page or an address: " + line.strip())

    def test_the_arc_user_name_stays_in_this_browser(self) -> None:
        # Section 12: kept only in this browser's localStorage, never sent to the server.
        self.assertEqual(self.code.count("'pcldb.arc_user'"), 1)
        self.assertEqual(self.code.count("localStorage"), 3)
        for needle in ("window.localStorage.getItem(ARC_USER_KEY)", "window.localStorage.setItem(ARC_USER_KEY, name)",
                       "window.localStorage.removeItem(ARC_USER_KEY)"):
            self.has(needle, 1)
        self.assertEqual(self.code.count("ARC_USER_KEY"), 4)   # the name and its three uses
        # What is kept is checked again when it is read back: another page of this site could
        # have written anything there.
        self.assertIn("userNameProblem(kept) === '' ? kept : ''", js_function(self.js, "readArcUser"))
        self.assertIn("const problem = userNameProblem(typed);", js_function(self.js, "onArcUserChange"))
        # The name is used in one place only: the commands. It is in no request.
        uses = [line.strip() for line in self.code.splitlines() if "arcUser" in line]
        self.assertEqual(sorted(uses), sorted([
            "arcUser: '',          // the typed ARC user name: kept here and by keepArcUser(), never in a request",
            "state.arcUser = '';",
            "state.arcUser = typed;",
            "state.arcUser = readArcUser();",
            "byId('arc-user').value = state.arcUser;",
            "const made = sshCommands(host, state.arcUser, arcPath);",
        ]))
        for name in ("api", "runAsk", "runSql", "start", "loadMe", "onSigninSubmit", "openDetail", "loadStatus",
                     "loadSchema", "loadExamples", "loadHistory"):
            body = js_function(self.js, name)
            self.assertNotIn("arc", body.lower().replace("search", ""), f"{name}() must not touch the ARC user name")

    def test_the_sign_in_code_leaves_the_address_first(self) -> None:
        take = self.code.index("function takeSigninCode()")
        body = self.code[take : self.code.index("async function start(", take)]
        self.assertIn("replaceAddress(window.location.pathname + window.location.search)", body)
        self.assertLess(body.index("replaceAddress("), body.index("return parsed.code"))
        self.has("window.history.replaceState(null, '', address);")
        self.assertEqual(self.code.count("replaceState"), 1, "the address is only changed in replaceAddress()")
        # When the browser refuses that, the address is still REPLACED (not added to the history).
        self.has("window.location.replace('#');", 1)
        self.assertEqual(self.code.count("location.replace("), 1)
        self.assertEqual(self.code.count("pushState"), 0)
        # It is read before anything else happens on the page, and before any request.
        boot = self.code[self.code.rindex("const code = takeSigninCode();") :]
        self.assertLess(boot.index("takeSigninCode()"), boot.index("start(code)"))
        self.assertLess(boot.index("takeSigninCode()"), boot.index("readStoredToken()"))
        # A code that fails gives a plain sentence and the form to paste a token.
        start = js_function(self.js, "start")
        self.assertIn("'That sign-in link was already used or is more than five minutes old.'", start)
        self.assertIn("' Paste your access token below, or start again with: pcldb serve --open'", start)
        self.assertIn("{anonymous: true}", start)
        # The code and the token it buys are checked for their shape before they are used.
        self.has("const CODE_SHAPE = /^[A-Za-z0-9_-]{16,128}$/;")
        self.has("const TOKEN_SHAPE = /^pcl_[A-Za-z0-9_-]{20,200}$/;")
        self.assertIn("if (!looksLikeToken(reply.token)) throw failed('badreply', 200, null);", start)

    def test_links_are_made_in_two_audited_places_only(self) -> None:
        self.assertEqual(self.code.count(".href ="), 2)
        self.has("link.href = safe;")        # outsideLink(): after safeGlobusUrl()
        self.has("link.href = address;")     # downloadCsv(): an object URL of this page
        outside = js_function(self.js, "outsideLink")
        for needle in ("const safe = safeGlobusUrl(address);", "if (safe === null) return null;",
                       "link.target = '_blank';", "link.rel = 'noopener noreferrer';", "link.href = safe;"):
            self.assertIn(needle, outside)
        self.assertLess(outside.index("if (safe === null) return null;"), outside.index("document.createElement('a')"))
        self.has("'_blank'", 1)
        self.has("safeGlobusUrl(address)", 1)
        # Elements are made in three places: el() with its two lists, and the two links.
        self.assertEqual(self.code.count("createElement("), 3)
        self.has("const node = document.createElement(tag);", 1)
        self.has("document.createElement('a')", 2)
        self.assertEqual(self.code.count("createElementNS"), 0)

    def test_el_makes_only_harmless_elements_and_attributes(self) -> None:
        tags = js_list(self.js, "EL_TAGS")
        attributes = js_list(self.js, "EL_ATTRIBUTES")
        self.assertEqual(sorted(tags), sorted(SAFE_TAGS), "a new element needs a look at this test first")
        self.assertEqual(sorted(attributes), sorted(SAFE_ATTRIBUTES), "a new attribute needs a look at this test first")
        for name in attributes:
            self.assertFalse(name.startswith("on"))
        make = js_function(self.js, "el")
        self.assertIn("if (EL_TAGS.indexOf(tag) === -1) throw new Error(", make)
        self.assertIn("if (EL_ATTRIBUTES.indexOf(name) === -1) throw new Error(", make)
        self.assertLess(make.index("EL_TAGS.indexOf(tag) === -1"), make.index("document.createElement(tag)"))
        self.assertLess(make.index("EL_ATTRIBUTES.indexOf(name) === -1"), make.index("node.setAttribute("))
        # Every element the script names is in the list, and the name is always written out
        # (never computed), so this check sees all of them.
        named = set(re.findall(r"\b(?:el|cutNode)\('([^']*)'", self.code))
        self.assertGreater(len(named), 20)
        self.assertEqual(named - SAFE_TAGS, set())
        self.assertEqual(re.findall(r"\b(?:el|cutNode)\((?!')[^)]", self.code.replace("function el(tag", "")
                                    .replace("function cutNode(tag", "").replace("return el(tag, all", "")), [])

    def test_text_reaches_the_page_in_three_audited_ways(self) -> None:
        # A text node, textContent, or a title: always through showHidden(), which writes out
        # characters that cannot be seen (a name can be made to read like another name with them).
        self.assertEqual(self.code.count(".textContent ="), 2)
        self.has("node.textContent = showHidden(text);", 1)      # setText()
        self.has("node.textContent = '';", 1)                    # clear()
        self.assertEqual(self.code.count("createTextNode("), 1)
        self.has("node.appendChild(document.createTextNode(showHidden(children)));", 1)
        self.has("else if (name === 'title' || name === 'aria-label') node.setAttribute(name, showHidden(value));", 1)
        # Attributes are set by el() or are one of four fixed names with fixed values.
        first_arguments = re.findall(r"setAttribute\(([^,]+),", self.code)
        self.assertEqual(sorted(set(first_arguments)),
                         ["'aria-busy'", "'aria-current'", "'aria-expanded'", "'open'", "name"])
        for value in re.findall(r"setAttribute\('aria-(?:busy|current|expanded)', ([^)]+)\)", self.code):
            self.assertRegex(value, r"^('page'|'true'|'false'|\w+ \? 'true' : 'false')$")
        self.has("dialog.setAttribute('open', '');", 1)
        # A class is a fixed name, never text from a record.
        self.assertEqual(sorted(set(re.findall(r"\.className = ([^;]+);", self.code))),
                         ["'btn btn-primary'", "String(value)"])
        self.assertEqual(sorted(set(re.findall(r"document\.title = ([^;]+);", self.code))),
                         ["'Sign in - PCL data'", "lookup(TAB_TITLES, tab, 'Normal') + ' - PCL data'"])
        # The one command that types into the query box types plain text.
        self.assertEqual(re.findall(r"execCommand\(([^)]*)\)", self.code), ["'insertText', false, text"])

    def test_the_views_follow_the_address_and_the_role(self) -> None:
        self.has("const TABS = Object.freeze(['normal', 'expert', 'status']);")
        self.has("const OLD_TAB_NAMES = Object.freeze({ask: 'normal', sql: 'expert'});")
        route = js_function(self.js, "route")
        self.assertIn("viewFor(parsed.kind === 'tab' ? parsed.tab : state.tab, asObject(state.me).capabilities)", route)
        self.assertIn("if (window.location.hash !== '#' + tab) replaceAddress('#' + tab);", route)
        self.assertIn("if (tab === 'expert') loadSqlSide();", route)
        self.assertIn("if (byPerson) byId(tab + '-title').focus();", route)
        self.assertIn("show(byId('tab-expert'), can('sql'));", js_function(self.js, "enterApp"))
        view_for = js_function(self.js, "viewFor")
        self.assertIn("if (tab === 'expert' && asArray(capabilities).indexOf('sql') === -1) return 'normal';", view_for)
        # The Expert view's loads never start anywhere else.
        self.has("loadSqlSide()", 2)      # its definition and route()
        self.has("loadSqlSide);", 1)      # the "Try again" button

    def test_every_element_the_script_asks_for_exists(self) -> None:
        page = parse_page()
        ids = set(page.ids())
        asked = set(re.findall(r"byId\('([^']+)'\)", self.js))
        self.assertGreater(len(asked), 30)
        for item in sorted(asked):
            with self.subTest(id=item):
                self.assertIn(item, ids)
        lines = set(re.findall(r"setLine\('([^']+)'", self.js))
        for line in ("ask-status", "sql-status", "schema-status", "history-status", "status-line", "arc-user-status"):
            self.assertIn(line, ids)
            self.assertIn(line, lines)
        # And the other way round: an id in the page that nothing uses is a leftover.
        used = asked | lines | {target for _tag, attrs in page.elements
                                for name in ("for", "aria-labelledby", "aria-describedby", "aria-controls")
                                for target in (attrs.get(name) or "").split()}
        for view in VIEWS:
            used |= {f"panel-{view}", f"tab-{view}", f"{view}-title"}
        used |= set(re.findall(r"'([a-z-]+)'", re.search(r"\[('user-name'[^\]]*)\]\.forEach", self.js).group(1)))
        self.assertEqual(sorted(ids - used - {"main"}), [])

    def test_the_normal_view(self) -> None:
        render = js_function(self.js, "renderAsk")
        # The answer is shown only when the assistant wrote it, under the label of section 12.
        self.assertIn("if (answerText !== '' && r.answer_by === 'llm') {", render)
        self.assertIn("'Written by the ARC assistant. Check it against the table.'", render)
        self.assertEqual(render.count("answer-text"), 1)
        how = js_function(self.js, "howSearched")
        for needle in ("'How this was searched'", "'Question read by'", "'Searched in'", "'The exact request that ran'"):
            self.assertIn(needle, how)
        # It never says "Globus Search" for the copy on this computer.
        self.has("if (backend === 'globus') return 'Globus Search';")
        self.has("if (backend === 'local') return \"this computer's copy of the index\";")
        self.has("if (plannedBy === 'llm') return 'assistant';")
        self.has("if (plannedBy === 'rules') return 'keyword rules';")
        self.has("const headings = ['Session', 'Instrument', 'Started (your time)', 'Samples', 'Labels', 'State'];")
        row = js_function(self.js, "sessionRow")
        self.assertIn("'simulated'", row)
        self.assertEqual(row.count("openDetail(path, folder, name, 'normal');"), 2)   # the name and the row

    def test_the_file_actions_follow_the_mode(self) -> None:
        actions = js_function(self.js, "fileActions")
        self.assertIn("if (lane !== 'expert') blocks.push(globusActions(session, statusNode));", actions)
        self.assertIn("if (lane === 'expert' || (lane === 'status' && can('sql'))) blocks.push(sshActions(session, statusNode));",
                      actions)
        self.has("openDetail(path, lastSegment(path), button, 'expert');", 1)    # from the result grid
        self.has("openDetail(path, lastSegment(path), name, 'status');", 1)      # from the registrations
        self.assertEqual(len(re.findall(r"openDetail\(", self.code)), 5)         # those four and the definition
        self.assertIn("append(body, detailContent(asObject(reply), lane));", js_function(self.js, "openDetail"))
        # Normal: "Open in Globus" (only the checked address) and "Copy Globus address".
        globus = js_function(self.js, "globusActions")
        self.assertIn("const link = outsideLink(session.web_url, 'Open in Globus');", globus)
        self.assertIn("copyButton('Copy Globus address', null, globusAddress, shown, statusNode, 'address')", globus)
        self.assertNotIn("storage_path", globus)
        # Expert: "Copy ARC path" and the three commands, or the sentence of section 12.
        ssh = js_function(self.js, "sshActions")
        self.assertIn("copyButton('Copy ARC path', null, arcPath, shown, statusNode, 'path')", ssh)
        self.assertIn("const host = textOf(asObject(asObject(state.me).storage).ssh_host);", ssh)
        self.assertIn("const made = sshCommands(host, state.arcUser, arcPath);", ssh)
        self.assertIn("'This path has unusual characters. Copy the path and quote it yourself.'", ssh)
        self.assertLess(ssh.index("if (made === null) {"), ssh.index("made.commands.map("))
        self.assertIn("return el('div', {class: 'lane-actions'}, parts);\n      }", ssh)   # leaves before the commands
        self.assertNotIn("web_url", ssh)
        self.assertNotIn("outsideLink", ssh)
        # The commands are built by the checked function and by nothing else.
        self.assertEqual(re.findall(r"'((?:scp|sftp|rsync)[^':]*) '", self.code), ["scp -r", "sftp", "rsync -av"])
        commands = js_function(self.js, "sshCommands")
        self.assertIn("if (!shellSafe(path) || !shellSafe(host) || !shellSafe(name)) return null;", commands)
        self.assertLess(commands.index("return null;"), commands.index("'scp -r '"))
        self.has("const SHELL_SAFE = /^[A-Za-z0-9._\\/@+-]+$/;")
        self.has("const USER_PLACEHOLDER = 'YOUR_ARC_USERNAME';")
        # What is copied is the value itself; what is shown has unseen characters written out.
        self.has("await navigator.clipboard.writeText(text);", 1)

    def test_the_result_grid(self) -> None:
        grid = js_function(self.js, "gridTable")
        self.assertIn("const lines = document.createDocumentFragment();", grid)
        self.assertIn("body.appendChild(lines);", grid)
        self.has("const GRID_MAX_ROWS = 2000;")
        self.has("const onPage = rows.length > GRID_MAX_ROWS ? rows.slice(0, GRID_MAX_ROWS) : rows;")
        # One listener for the whole grid, however many rows it has.
        self.has("byId('sql-result').addEventListener('click', onResultClick);", 1)
        cell = js_function(self.js, "gridCell")
        self.assertNotIn("addEventListener", cell)
        self.assertNotIn("addEventListener", grid)
        self.assertIn("const path = sessionPathOfCell(columnName, value);", cell)
        self.assertIn("paths.set(button, path);", cell)
        path_of = js_function(self.js, "sessionPathOfCell")
        self.assertIn("if (columnName !== 'path' && columnName !== 'session_path') return null;", path_of)
        self.assertIn("return /^[A-Za-z0-9]/.test(value) ? value : null;", path_of)
        # NULL, arrays and objects are shown as text.
        self.has("if (value === null || value === undefined) return {text: 'NULL', kind: 'null'};")
        self.has("return {text: jsonText(value), kind: 'json'};")
        for needle in ("rowsLine(", "truncatedLine(", "'Download CSV'"):
            self.assertIn(needle, js_function(self.js, "renderSqlResult"))

    def test_csv_download_details(self) -> None:
        self.has("'\\ufeff'")                       # the byte order mark for Excel
        self.has("lines.join('\\r\\n')")            # RFC 4180 line ends
        self.has("type: 'text/csv;charset=utf-8'")
        download = js_function(self.js, "downloadCsv")
        self.assertIn("const address = URL.createObjectURL(blob);", download)
        self.assertIn("window.setTimeout(function () { URL.revokeObjectURL(address); }, 10000);", download)
        self.assertIn("document.body.removeChild(link);", download)
        self.assertEqual(self.code.count("createObjectURL("), 1)
        self.assertEqual(self.code.count("revokeObjectURL("), 1)
        # The link is for a download only: it is never shown and never opens a tab.
        self.assertIn("link.hidden = true;", download)
        self.assertNotIn("target", download)

    def test_the_sql_editor_keys(self) -> None:
        for needle in ("event.key === 'Enter' && (event.ctrlKey || event.metaKey)", "event.key === 'Tab'",
                       "event.key === 'Escape'", "insertIntoEditor('  ')"):
            self.has(needle)
        self.has("if (state.sqlRun) return;    // one query at a time")

    def test_the_detail_panel_gives_the_focus_back(self) -> None:
        closed = js_function(self.js, "afterDetailClosed")
        self.assertIn("if (opener && document.contains(opener)) opener.focus();", closed)
        self.assertIn("else if (state.me) byId(state.tab + '-title').focus();", closed)
        self.has("dialog.addEventListener('close', afterDetailClosed);", 1)
        self.has("if (typeof dialog.showModal === 'function') dialog.showModal();", 1)

    def test_the_words_of_the_script_are_plain(self) -> None:
        sentences = js_sentences(self.js)
        self.assertGreater(len(sentences), 80)
        for word in JARGON:
            pattern = re.compile(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])")
            for sentence in sentences:
                with self.subTest(word=word):
                    self.assertIsNone(pattern.search(sentence), sentence)
        for sentence in sentences:
            # The views are Normal and Expert; none is called a "tab" (only the browser's own tabs are).
            self.assertIsNone(re.search(r"\b(Ask|SQL|Status|Normal|Expert|this|the) tab\b", sentence), sentence)
        # Exact sentences that other parts of the specification give.
        for needle in ("'Written by the ARC assistant. Check it against the table.'",
                       "'This path has unusual characters. Copy the path and quote it yourself.'",
                       "'These are made-up demo sessions, not lab data.'",
                       "'A shared server needs an answer from ARC about keys first.'"):
            self.has(needle)
        # What the Status view says about the search is true for both backends.
        self.has("\"Questions in Normal mode are searched in this computer's copy of the index. "
                 "Globus Search is not in use yet.\"")
        self.has("'Questions in Normal mode are searched with Globus Search, a hosted index that holds one small "
                 "entry for each session.'")


class TestAppCss(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = read("app.css")
        cls.clean = strip_css_comments(cls.css)

    def test_nothing_is_loaded_from_anywhere(self) -> None:
        lowered = self.clean.lower()
        for needle in ("@import", "url(", "expression(", "http:", "https:", "@font-face", "@namespace",
                       "-moz-binding", "image-set(", "src:"):
            self.assertFalse(needle in lowered, f"{needle!r} is in app.css")
        self.assertIsNone(re.search(r"(?<![-\w])behavior\s*:", lowered), "the old way to run script from a style sheet")

    def test_system_fonts_and_the_basics(self) -> None:
        for needle in ("system-ui", "ui-monospace", ":focus-visible", "@media (prefers-color-scheme: dark)",
                       "@media (prefers-reduced-motion: reduce)", "@media (forced-colors: active)", "[hidden]",
                       "position: sticky"):
            self.assertTrue(needle in self.clean, f"{needle!r} is not in app.css")
        light, _dark = css_variables(self.css)
        for name in ("bg", "surface", "text", "muted", "border", "accent", "danger", "warning"):
            self.assertIn(name, light)
        for name in ("--radius:", "--space-1:", "--space-4:", "--font:", "--mono:"):
            self.assertTrue(name in self.clean, f"{name!r} is not in app.css")

    def test_a_hidden_element_is_never_shown(self) -> None:
        # The Expert entry and view are hidden with the "hidden" attribute. A rule such as
        # ".tab { display: inline-block }" would show them again without this line.
        self.assertRegex(self.clean, r"\[hidden\]\s*\{\s*display:\s*none\s*!important;\s*\}")

    def test_sizes_follow_the_readers_settings(self) -> None:
        # Text sizes in rem or em (they follow the browser's text size and zoom), and no
        # fixed width that could not fit a 380 px screen.
        for size in re.findall(r"font-size:\s*([^;]+);", self.clean):
            with self.subTest(size=size):
                self.assertRegex(size.strip(), r"^(\d*\.?\d+(rem|em)|100%)$")
        rules = re.sub(r"@media[^{]*\{", "", self.clean)
        for width in re.findall(r"(?<![-\w])(?:min-)?width:\s*(\d+)px", rules):
            self.assertLessEqual(int(width), 380, f"a width of {width}px")
        for width in re.findall(r"(?<![-\w])min-width:\s*(\d+(?:\.\d+)?)rem", rules):
            # 46rem tables live inside boxes that scroll; nothing else may be wider than a phone.
            self.assertLessEqual(float(width), 46.0)
        for media in re.findall(r"@media\s*\((?:max|min)-width:\s*(\d+)px\)", self.clean):
            self.assertGreater(int(media), 380, "a narrow layout must already hold at 380 px")
        self.assertNotIn("user-select: none", self.clean)
        self.assertNotIn("outline: none;\n}\n\n.btn", self.clean)

    def test_no_motion_for_people_who_ask_for_none(self) -> None:
        block = self.clean[self.clean.index("@media (prefers-reduced-motion: reduce)") :]
        for needle in ("animation-duration: 0.01ms !important;", "transition-duration: 0.01ms !important;",
                       "animation: none;"):
            self.assertIn(needle, block)
        # The only animation is the mark of a running request.
        self.assertEqual(re.findall(r"@keyframes\s+(\w+)", self.clean), ["spin"])

    def test_colours_are_only_set_through_the_custom_properties(self) -> None:
        without_root = re.sub(r":root\s*\{[^}]*\}", "", self.clean)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", without_root), [])
        # Apart from the two shades of black: the dimmed page behind the panel and the hover
        # shade of the main button.
        self.assertEqual(sorted(set(re.findall(r"rgba?\([^)]*\)", without_root))),
                         ["rgba(0, 0, 0, 0.14)", "rgba(0, 0, 0, 0.55)"])
        self.assertEqual(re.findall(r"\b(?:hsla?|hwb|lab|lch|oklab|oklch|color-mix)\(", without_root), [])
        named = re.findall(r"(?:color|background|border(?:-\w+)*-color):\s*(?!var\(|inherit|transparent|none|currentColor|"
                           r"Canvas|Highlight)([a-zA-Z]+)\s*;", without_root)
        self.assertEqual(named, [], "a colour by name does not change with the colour scheme")

    def test_every_colour_pair_in_use_is_tested(self) -> None:
        # Each rule that sets a text colour from a custom property: the colour must be one of
        # the tested text colours, so a new colour cannot slip past the contrast test.
        used = set(re.findall(r"(?<![-\w])color:\s*var\(--([\w-]+)\)", self.clean))
        self.assertEqual(used - {front for front, _back in TEXT_PAIRS}, set())
        backgrounds = set(re.findall(r"background(?:-color)?:\s*var\(--([\w-]+)\)", self.clean))
        tested = {back for _front, back in TEXT_PAIRS} | {"accent"}
        self.assertEqual(backgrounds - tested, set())

    def test_contrast_is_at_least_wcag_aa_in_both_schemes(self) -> None:
        for scheme, colours in zip(("light", "dark"), css_variables(self.css)):
            for pairs, least in ((TEXT_PAIRS, 4.5), (EDGE_PAIRS, 3.0)):
                for front, back in pairs:
                    with self.subTest(scheme=scheme, front=front, back=back):
                        self.assertGreaterEqual(contrast(colours[front], colours[back]), least)
            # The main button under the pointer: its colour under 14 % black.
            with self.subTest(scheme=scheme, pair="the main button under the pointer"):
                self.assertGreaterEqual(contrast(colours["accent-text"], darkened(colours["accent"], 0.14)), 4.5)

    def test_both_schemes_define_the_same_colours(self) -> None:
        clean = self.clean
        light = set(re.findall(r"--([\w-]+):\s*#", re.search(r":root\s*\{([^}]*)\}", clean).group(1)))
        dark_block = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}", clean)
        dark = set(re.findall(r"--([\w-]+):\s*#", dark_block.group(1)))
        self.assertEqual(light, dark)
        self.assertIn("color-scheme: light dark;", clean)


class TestClassesMatch(unittest.TestCase):
    """A class named in the page or the script has a rule, and every rule is used."""

    def test_classes_in_page_and_script_have_rules(self) -> None:
        styled = css_classes(read("app.css"))
        for name in sorted(parse_page().classes() | js_classes(read("app.js"))):
            with self.subTest(css_class=name):
                self.assertIn(name, styled)

    def test_every_rule_is_used(self) -> None:
        js_words: set[str] = set()
        for literal in js_string_literals(read("app.js")):
            js_words.update(literal.split())
        used = parse_page().classes() | js_words
        for name in sorted(css_classes(read("app.css"))):
            with self.subTest(css_class=name):
                self.assertIn(name, used)


class TestFavicon(unittest.TestCase):
    def test_it_is_a_plain_drawing(self) -> None:
        svg = read("favicon.svg")
        self.assertTrue(svg.startswith("<svg "))
        lowered = svg.lower()
        for needle in ("<script", "href", "style", "<foreignobject", "<image", "<use", "javascript:", "url(",
                       "<!entity", "<!doctype", "<animate", "<set"):
            self.assertNotIn(needle, lowered)
        self.assertIsNone(re.search(r"\son\w+\s*=", lowered), "no event attributes")
        # The only address in it is the SVG namespace name, which nothing ever fetches.
        self.assertEqual(re.findall(r"https?://[^\"'\s>]+", svg), ["http://www.w3.org/2000/svg"])


class TestWithNode(unittest.TestCase):
    """Skipped with a reason when Node is not installed (the website itself never needs it)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("Node is not on PATH: the JavaScript syntax check, the tests of the pure "
                                    "functions and the tests of the page were not run")

    def run_node(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.node, *arguments], capture_output=True, text=True, timeout=180,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False)

    def check_run(self, script: Path) -> None:
        done = self.run_node(str(script))
        self.assertEqual(done.returncode, 0, "\n" + done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"\n(\d+) of \1 passed\n?$")
        self.assertNotIn("FAILED", done.stdout)

    def test_syntax(self) -> None:
        for path in (WEB / "app.js", NODE_PURE_TESTS, NODE_PAGE_TESTS, NODE_REPLAY_TESTS):
            with self.subTest(file=path.name):
                done = self.run_node("--check", str(path))
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_pure_functions(self) -> None:
        self.check_run(NODE_PURE_TESTS)

    def test_the_page_on_a_stand_in_browser(self) -> None:
        self.check_run(NODE_PAGE_TESTS)


# ---------------------------------------------------------------------- the page and the real server

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The questions whose replies tests/web_replay_test.js needs, with include_simulated; the example
# questions of the page are added to them.
REPLAY_QUESTIONS = (
    ("newest sessions", True),
    ("How many sessions per instrument?", True),
    ("Which sessions have adenine?", False),
    ("zzqx unheardofword", True),
)
VIEWER_QUESTIONS = (("Which sessions still need review?", True),)

# The queries of the Expert tests: name -> SQL that both engines run.
REPLAY_SQL = {
    "sql_small": "SELECT path, instrument, started_utc, simulated, sample_ids, card_json, bytes "
                 "FROM sessions ORDER BY started_utc DESC, path",
    "sql_paths": "SELECT session_path, label, n FROM session_labels ORDER BY session_path, label",
    "sql_big": ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1200) "
                "SELECT (SELECT path FROM sessions ORDER BY path LIMIT 1) AS path, "
                "'<script>alert(' || i || ')</script>' AS session_path, "
                + ", ".join("i AS c%02d" % k if k % 3 == 0 else "'v' || i || '_%d' AS c%02d" % (k, k)
                            for k in range(1, 29))
                + " FROM n"),
    "sql_error": "SELECT nosuchcolumn FROM sessions",
    "sql_refused": "DELETE FROM sessions",
    "sql_private": "SELECT * FROM app.users",
}


def hostile_document():
    """A registration document whose card holds markup, quotes, a direction override and about
    100 kB of text, under a path with spaces and shell characters (made-up values only)."""
    from pcldb import util
    from tests import registry_support as rs
    now = util.now_utc_text()           # the newest session, so it is on top of "newest sessions"
    card = rs.make_card(
        session="hostile1", session_id="hostile-0001", started_utc=now, uploaded_utc=now,
        ended_utc=None,
        sample_ids=["<img src=x onerror=alert(1)>", "\"quoted\" & 'single'"],
        operator="demo-operator-a\"><svg onload=alert(1)>",
        location="<script>alert('loc')</script>",
        notes="<script>alert(\"notes\")</script> \u202eevil\u202c & <b>bold</b> " + "N" * 19000,
        method="\"; DROP TABLE x; --",
        extra={"big": "B" * 100000, "html": "<iframe src=javascript:alert(1)>"})
    return rs.make_document(path="uploads/odd $(touch pwned) folder;`id`/run 1", card=card,
                            upload=rs.make_upload(transport="globus",
                                                  check_method="globus-checksum"))


def capture_replies(base_url, code, viewer_token):
    """Sign in with the one-time code as the page does, call every endpoint the page uses (and
    register the hostile session first), and return {"code", "records"} for
    tests/web_replay_test.js. The tokens are not in the result."""
    records = []

    def call(name, method, path, body=None, token=None, who="admin", session_path=None):
        request = urllib.request.Request(base_url + path, method=method)
        request.add_header("Accept", "application/json")
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with _OPENER.open(request, data=data, timeout=120) as answer:
                status, raw = answer.status, answer.read()
                content_type = answer.headers.get("Content-Type")
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
            content_type = error.headers.get("Content-Type")
        if not (content_type or "").startswith("application/json"):
            raise AssertionError("%s %s answered %s, not JSON" % (method, path, content_type))
        payload = json.loads(raw.decode("utf-8"))
        records.append({"name": name, "as": who, "method": method, "path": path.split("?")[0],
                        "request": body, "session_path": session_path, "status": status,
                        "body": payload})
        return status, payload

    status, signed = call("signin", "POST", "/api/v1/signin", {"code": code}, who="*")
    if status != 200:
        raise AssertionError("sign-in failed: %r" % (signed,))
    admin = signed["token"]
    records[-1]["body"]["token"] = None             # the token never goes into the file
    call("signin_again", "POST", "/api/v1/signin", {"code": code}, who="*")
    call("me", "GET", "/api/v1/me", token=admin)
    call("me_viewer", "GET", "/api/v1/me", token=viewer_token, who="viewer")
    call("me_bad_token", "GET", "/api/v1/me", token="pcl_" + "A" * 43, who="bad")
    call("register_hostile", "POST", "/api/v1/registrations", {"document": hostile_document()},
         token=admin)
    call("status", "GET", "/api/v1/status", token=admin)
    call("status_viewer", "GET", "/api/v1/status", token=viewer_token, who="viewer")
    questions = [(q, True) for q in EXAMPLE_QUESTIONS]
    questions += [item for item in REPLAY_QUESTIONS if item not in questions]
    for number, (question, simulated) in enumerate(questions):
        call("ask_%02d" % number, "POST", "/api/v1/ask",
             {"question": question, "include_simulated": simulated}, token=admin)
    for number, (question, simulated) in enumerate(VIEWER_QUESTIONS):
        call("ask_viewer_%02d" % number, "POST", "/api/v1/ask",
             {"question": question, "include_simulated": simulated}, token=viewer_token,
             who="viewer")
    call("sql_schema", "GET", "/api/v1/sql/schema", token=admin)
    call("sql_examples", "GET", "/api/v1/sql/examples", token=admin)
    status, listed = call("sql_all_paths", "POST", "/api/v1/sql",
                          {"sql": "SELECT path FROM sessions ORDER BY path", "max_rows": 1000},
                          token=admin)
    for number, (path,) in enumerate(listed["rows"]):
        call("session_%02d" % number, "GET",
             "/api/v1/session?path=" + urllib.parse.quote(path, safe=""), token=admin, who="*",
             session_path=path)
    call("session_missing", "GET", "/api/v1/session?path=raw%2Framan-portable%2Fnone",
         token=admin)
    for name, sql in REPLAY_SQL.items():
        call(name, "POST", "/api/v1/sql", {"sql": sql, "max_rows": 1000}, token=admin)
    call("sql_history", "GET", "/api/v1/sql/history", token=admin)
    call("sql_viewer_403", "POST", "/api/v1/sql", {"sql": "SELECT 1", "max_rows": 1000},
         token=viewer_token, who="viewer")
    for name, path in (("sql_schema_viewer_403", "/api/v1/sql/schema"),
                       ("sql_examples_viewer_403", "/api/v1/sql/examples"),
                       ("sql_history_viewer_403", "/api/v1/sql/history")):
        call(name, "GET", path, token=viewer_token, who="viewer")
    text = json.dumps(records)
    if admin in text or viewer_token in text:
        raise AssertionError("a token is in the recorded replies")
    return {"code": code, "records": records}


class PageOnTheRealBackendCases:
    """pcldb's server in a thread, on demo data, with an assistant that is switched on and
    cannot be reached (so questions are read by the keyword rules WITH a notice), then the page
    on the stand-in browser with what the server answered."""

    settings_changes = {"ask.per_user_per_minute": 1000}

    def setUp(self) -> None:
        super().setUp()
        self.node = shutil.which("node")
        if self.node is None:
            self.skipTest("Node is not on PATH: the page was not run against the real backend")
        from pcldb import ask, auth, demo, llm, reconcile, server
        ask.clear_caches()
        with self.store.app() as db, tempfile.TemporaryDirectory(prefix="pcldb-") as folder:
            # The demo's sessions loaded as an ordinary index (not as "the demo"), so that the
            # hostile session can be registered beside them: while the demo itself is loaded,
            # registrations are refused (demo and real data never mix).
            index = Path(folder) / "demo_sessions.sqlite"
            demo.build_demo_index(index)
            reconcile.load_index(db, index, actor=demo.DEMO_ACTOR, settings=self.settings,
                                 source="index")
            if auth.find_user(db, "admin") is None:
                auth.create_user(db, "admin", "admin", display_name="Local admin")
            auth.create_user(db, "demo-reviewer", "viewer")
            self.viewer_token = auth.issue_token(db, "demo-reviewer", label="tests")
        unreachable = llm.ChatClient("http://127.0.0.1:%d/api/v1" % support.free_port(),
                                     "not-a-real-key", "gpt-oss-120b", timeout=5,
                                     sleep=lambda seconds: None)
        self.server = server.make_server(self.settings, host="127.0.0.1", port=0,
                                         store=self.store, llm=unreachable)
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            thread.join(10)

        self.addCleanup(stop)
        self.base = "http://127.0.0.1:%d" % self.server.port

    def test_the_page_against_what_the_server_answers(self) -> None:
        code = self.server.app.signin.new()
        replies = capture_replies(self.base, code, self.viewer_token)
        notices = [r for r in replies["records"] if r["path"] == "/api/v1/ask"
                   and r["body"].get("notices")]
        self.assertTrue(notices, "an assistant that cannot be reached gives a notice")
        with tempfile.TemporaryDirectory(prefix="pcldb replay ") as folder:
            path = Path(folder) / "replies.json"
            path.write_text(json.dumps(replies), encoding="utf-8")
            done = subprocess.run([self.node, str(NODE_REPLAY_TESTS), str(path)],
                                  capture_output=True, text=True, timeout=300, encoding="utf-8",
                                  errors="replace", stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(done.returncode, 0, "\n" + done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"\n(\d+) of \1 passed\n?$")


class TestPageOnTheRealBackendSqlite(PageOnTheRealBackendCases, support.SqliteCase,
                                     unittest.TestCase):
    pass


class TestPageOnTheRealBackendPostgres(PageOnTheRealBackendCases, support.PostgresCase,
                                       unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
