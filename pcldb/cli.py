"""The command line: `python -m pcldb <command>` (specification section 13).

Every command is declared here with argparse. A command's work lives in the module of the part
it belongs to; that module is imported only when the command runs (`COMMANDS` says which
function). While a part is not there yet, its command says so in one sentence and ends with 1.

Exit codes: 0 = done, 1 = something a person can put right (the sentence is on stderr, never a
stack trace), 2 = the command was not typed correctly.

Everything printed is ASCII, so it reads the same in a Windows console.

`cmd_setup` and `cmd_db` are implemented here. Every command function has the signature
`cmd_<name>(args, settings) -> int`; the names of `args` are the argparse `dest` names below.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys

from . import __version__
from .errors import PclError

# command -> (module inside the pcldb package, function). Imported when the command runs.
COMMANDS = {
    "setup": ("cli", "cmd_setup"),
    "db": ("cli", "cmd_db"),
    "serve": ("server", "cmd_serve"),
    "doctor": ("doctor", "cmd_doctor"),
    "user": ("auth", "cmd_user"),
    "token": ("auth", "cmd_token"),
    "register": ("registration", "cmd_register"),
    "load-index": ("reconcile", "cmd_load_index"),
    "demo": ("demo", "cmd_demo"),
    "dump": ("transfer", "cmd_dump"),
    "restore": ("transfer", "cmd_restore"),
    "export-sqlite": ("transfer", "cmd_export_sqlite"),
    "search": ("search_sync", "cmd_search"),
    "ask": ("ask", "cmd_ask"),
    "sql": ("sqlrun", "cmd_sql"),
}

ENGINE_CHOICES = ("postgresql", "sqlite")
ROLE_CHOICES = ("viewer", "analyst", "uploader", "admin")
DB_ACTIONS = ("status", "start", "stop", "reset")
USER_ACTIONS = ("add", "list", "role", "disable", "enable")
TOKEN_ACTIONS = ("new", "list", "revoke")
DEMO_ACTIONS = ("load", "clear")
SEARCH_ACTIONS = ("status", "push", "reindex", "login", "logout", "create-index", "query",
                  "use-globus", "use-local")

ADMIN_NAME = "admin"
ADMIN_DISPLAY_NAME = "Local admin"
ADMIN_TOKEN_FILE = "admin_token.txt"

EXIT_OK, EXIT_PROBLEM, EXIT_USAGE = 0, 1, 2


# ---------------------------------------------------------------------- printing


def _plain(text):
    """Text for the console: ASCII only. Line breaks stay; other control characters go."""
    text = "".join(char if char == "\n" or " " <= char != "\x7f" else " " for char in str(text))
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _say(text="", stream=None):
    print(_plain(text), file=stream or sys.stdout)


def _problem(text):
    _say(text, sys.stderr)


# ---------------------------------------------------------------------- argument types


def _whole_number(low, high, what):
    def convert(text):
        try:
            value = int(text, 10)
        except ValueError:
            value = None
        if value is None or not low <= value <= high:
            raise argparse.ArgumentTypeError(
                "%r is not %s (a whole number from %d to %d)" % (text, what, low, high))
        return value
    return convert


_port = _whole_number(1, 65535, "a port")
_row_count = _whole_number(1, 1000000, "a number of rows")


def _days(text):
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is None or not 0 < value <= 3650:
        raise argparse.ArgumentTypeError(
            "%r is not a number of days (above 0, at most 3650; 0.5 is twelve hours)" % (text,))
    return int(value) if value == int(value) else value


def _seconds(text):
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is None or not 0 <= value <= 3600:
        raise argparse.ArgumentTypeError(
            "%r is not a number of seconds from 0 to 3600" % (text,))
    return int(value) if value == int(value) else value


# ---------------------------------------------------------------------- the commands, declared


_TOP_TEXT = """\
pcldb keeps the lab's register of instrument sessions on this computer and answers
questions about it: in plain English (normal mode) or in SQL (expert mode).

First time here:
  pcldb setup            make the database and the admin user
  pcldb serve --open     start the website and open it in the browser
  pcldb doctor           check that everything is in place
From the project folder, type ./pcldb.sh (macOS, Linux) or .\\pcldb.cmd (Windows)
where this help writes pcldb.
"""

_TOP_END = """\
Help for one command: pcldb COMMAND --help
A token is never typed on the command line. Tools read it from the variable PCLDB_TOKEN,
else from the file given with --token-file, else from var/admin_token.txt.
"""


def build_parser():
    """The argparse parser with every command of the specification."""
    parser = argparse.ArgumentParser(
        prog="pcldb", description=_TOP_TEXT, epilog=_TOP_END,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dir", dest="dir", metavar="PATH", default=None,
        help="the project folder to work in (the same as the variable PCLDB_DIR). Default: "
             "the folder that holds pcldb. Write it before the command.")
    parser.add_argument("--version", action="version", version="pcldb " + __version__)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", title="commands")

    def add(name, summary, details="", examples=()):
        text = summary[0].upper() + summary[1:] + "."
        if details:
            text += "\n\n" + details
        epilog = None
        if examples:
            epilog = "Examples:\n" + "\n".join("  " + example for example in examples)
        return commands.add_parser(name, help=summary, description=text, epilog=epilog,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)

    # -- setup --------------------------------------------------------------------------------
    setup = add(
        "setup", "make the settings file, the database and the admin user",
        "Safe to run again: it only makes what is missing. It never changes a settings file\n"
        "that exists, and it never makes a second admin.\n\n"
        "Without --engine it takes PostgreSQL when PostgreSQL's programs are on this computer,\n"
        "else SQLite (two plain files, nothing to install), and says which it took.\n"
        "The admin's token goes into var/admin_token.txt. It is not shown on the screen.",
        ["pcldb setup", "pcldb setup --engine sqlite --demo"])
    setup.add_argument("--engine", dest="engine", choices=ENGINE_CHOICES, default=None,
                       help="where the records are kept: postgresql or sqlite")
    setup.add_argument("--demo", dest="demo", action="store_true",
                       help="also load a small set of made-up sessions to try things out")
    setup.add_argument("--port", dest="port", type=_port, metavar="N", default=None,
                       help="the port of the local PostgreSQL database on this computer "
                            "(default 15439). Only used when the settings file is made now.")

    # -- db -----------------------------------------------------------------------------------
    db = add(
        "db", "look at, start, stop or empty the local database",
        "status  say where the database is and whether it runs\n"
        "start   start the local PostgreSQL database (SQLite has nothing to start)\n"
        "stop    stop the local PostgreSQL database\n"
        "reset   delete every record, user and token of this engine and start empty.\n"
        "        Needs --yes. The files on ARC are never touched.",
        ["pcldb db status", "pcldb db reset --yes"])
    db.add_argument("action", choices=DB_ACTIONS, help="what to do")
    db.add_argument("--yes", dest="yes", action="store_true",
                    help="really delete everything (only for reset)")

    # -- serve --------------------------------------------------------------------------------
    serve = add(
        "serve", "start the website and the API on this computer",
        "Prints the address to open. The address holds a one-time sign-in code, so the browser\n"
        "is signed in as the local admin without typing a token. Stop with Ctrl+C.",
        ["pcldb serve --open"])
    serve.add_argument("--open", dest="open_browser", action="store_true",
                       help="open the website in the browser")
    serve.add_argument("--host", dest="host", default=None, metavar="H",
                       help="the address to listen on (default: the setting server.host, "
                            "127.0.0.1)")
    serve.add_argument("--port", dest="port", type=_port, default=None, metavar="N",
                       help="the port to listen on (default: the setting server.port, 8787)")

    # -- doctor -------------------------------------------------------------------------------
    add("doctor", "check this installation, one line per check",
        "Each line says OK or PROBLEM, and for a problem what to do about it: Python, the\n"
        "packages, the database, the expert-mode sandbox, the search queue, the ARC assistant\n"
        "and the website's port.")

    # -- user ---------------------------------------------------------------------------------
    user = add(
        "user", "add people and say what they may do",
        "add      add a user (role viewer unless --role is given)\n"
        "list     show every user, or the one that is named\n"
        "role     give a user another role\n"
        "disable  switch a user off: its tokens stop working\n"
        "enable   switch a user on again\n\n"
        "Roles:   viewer    ask questions and search\n"
        "         analyst   the same, and expert SQL\n"
        "         uploader  ask and search, and register sessions\n"
        "         admin     everything, and manage users\n"
        "User names: lower-case letters, digits and . _ - (at most 64 characters).",
        ["pcldb user add demo-operator-a --role uploader --display-name \"Demo operator A\"",
         "pcldb user list", "pcldb user role demo-operator-a --role analyst"])
    user.add_argument("action", choices=USER_ACTIONS, help="what to do")
    user.add_argument("name", nargs="?", default=None, help="the user's name")
    user.add_argument("--role", dest="role", choices=ROLE_CHOICES, default=None,
                      help="the role (for add and role)")
    user.add_argument("--display-name", dest="display_name", default=None, metavar="TEXT",
                      help="the name people see (for add)")

    # -- token --------------------------------------------------------------------------------
    token = add(
        "token", "make, list or revoke the tokens that people and tools sign in with",
        "new     make a token for a user. It is saved in a private file under var/tokens/\n"
        "        and never shown on the screen; the command says which file.\n"
        "list    show the tokens (never their text): id, user, state, dates\n"
        "revoke  stop one token from working, for good",
        ["pcldb token new --user demo-operator-a --label \"upload laptop\" --days 90",
         "pcldb token list --user demo-operator-a",
         "pcldb token revoke --id 0123456789abcdef0123456789abcdef"])
    token.add_argument("action", choices=TOKEN_ACTIONS, help="what to do")
    token.add_argument("--user", dest="user", default=None, metavar="NAME",
                       help="whose token (needed for new; narrows list)")
    token.add_argument("--label", dest="label", default=None, metavar="TEXT",
                       help="a note that says what the token is for (for new)")
    token.add_argument("--days", dest="days", type=_days, default=None, metavar="N",
                       help="how many days the token works (for new). Left out: it does not "
                            "expire.")
    token.add_argument("--id", dest="token_id", default=None, metavar="ID",
                       help="the token's id, as shown by list (for revoke)")

    # -- register -----------------------------------------------------------------------------
    register = add(
        "register", "put sessions that are on ARC into the register",
        "Give exactly one source:\n"
        "  FOLDER --path REL       one session folder on this computer that is a copy of what\n"
        "                          is on ARC; REL is its place below the project folder\n"
        "  --tree ROOT             every session of a folder tree laid out like ARC\n"
        "  --document FILE         one registration document (JSON)\n"
        "  --raman-uploads HOME    every checked upload of the Raman controller in HOME\n\n"
        "Prints one line per session (created, unchanged, or refused and why) and a total.\n"
        "With --server (or the variable PCLDB_SERVER_URL) it goes through a running pcldb\n"
        "server and needs a token; without, it writes straight into the local database.",
        ["pcldb register ./1d_y_scan_20261116_225015_281 "
         "--path raw/raman-portable/simulated/1d_y_scan_20261116_225015_281",
         "pcldb register --tree ./arc_copy", "pcldb register --document session.json"])
    register.add_argument("folder", nargs="?", default=None, metavar="FOLDER",
                          help="a session folder on this computer (use with --path)")
    register.add_argument("--path", dest="path", default=None, metavar="REL",
                          help="where FOLDER is below the project folder on ARC, for example "
                               "raw/raman-portable/<session folder>")
    register.add_argument("--card", dest="card", default=None, metavar="FILE",
                          help="the session's card, when it is not pcl_session.json in FOLDER")
    register.add_argument("--replace", dest="replace", action="store_true",
                          help="replace a session that is registered with other contents")
    register.add_argument("--tree", dest="tree", default=None, metavar="ROOT",
                          help="a folder tree laid out like the project folder on ARC")
    register.add_argument("--document", dest="document", default=None, metavar="FILE",
                          help="a registration document (JSON file)")
    register.add_argument("--raman-uploads", dest="raman_uploads", default=None,
                          metavar="CONTROLLER_HOME",
                          help="the Raman controller's home folder; its checked uploads are "
                               "registered")
    register.add_argument("--instrument", dest="instrument", default="raman-portable",
                          metavar="SLUG",
                          help="the instrument of --raman-uploads (default raman-portable)")
    register.add_argument("--scan-sessions", dest="scan_sessions", default=None, metavar="DIR",
                          help="the controller's folder of scan sessions, to also take "
                               "operator, location and notes (for --raman-uploads)")
    register.add_argument("--server", dest="server", default=None, metavar="URL",
                          help="register through this pcldb server, for example "
                               "http://127.0.0.1:8787")
    register.add_argument("--token-file", dest="token_file", default=None, metavar="FILE",
                          help="a file whose one line is the token for the server")

    # -- load-index ---------------------------------------------------------------------------
    load_index = add(
        "load-index", "load the nightly index file made on ARC",
        "FILE is the sessions.sqlite written by the nightly index build. Sessions, analysis\n"
        "runs, labels, reviews and models are taken from it.",
        ["pcldb load-index sessions.sqlite"])
    load_index.add_argument("file", metavar="FILE", help="the index file (sessions.sqlite)")

    # -- demo ---------------------------------------------------------------------------------
    demo = add(
        "demo", "load or remove the made-up demo sessions",
        "load   put about 16 clearly made-up sessions into an empty database\n"
        "clear  remove them again",
        ["pcldb demo load", "pcldb demo clear"])
    demo.add_argument("action", choices=DEMO_ACTIONS, help="what to do")

    # -- dump, restore, export-sqlite ---------------------------------------------------------
    dump = add(
        "dump", "write every record into one file",
        "The file can be loaded into either engine with: pcldb restore FILE\n"
        "That is the way to move from SQLite to PostgreSQL or back.",
        ["pcldb dump pcl_dump.jsonl"])
    dump.add_argument("file", metavar="FILE", help="the file to write")

    restore = add(
        "restore", "load a file written by pcldb dump into an empty database",
        "Users and tokens are left out unless --include-users is given.",
        ["pcldb restore pcl_dump.jsonl"])
    restore.add_argument("file", metavar="FILE", help="the file to read")
    restore.add_argument("--include-users", dest="include_users", action="store_true",
                         help="also load the users and their tokens (they replace the "
                              "admin that pcldb setup made)")

    export = add(
        "export-sqlite", "write the catalogue as one SQLite file",
        "The file holds everything expert mode can read. It holds no users, tokens or\n"
        "logs, but it does hold operator names and notes: keep it inside the lab's\n"
        "project folder. Copied there on ARC, it can be queried with sqlite3, without\n"
        "any server.",
        ["pcldb export-sqlite pcl_catalog_copy.sqlite"])
    export.add_argument("file", metavar="FILE", help="the file to write")

    # -- search -------------------------------------------------------------------------------
    search = add(
        "search", "look after the search index (the local one and Globus Search)",
        "status        how many entries wait, are sent or have failed, and who answers\n"
        "push          send the waiting entries to Globus Search\n"
        "reindex       build every entry again with the current settings and queue it\n"
        "login         sign in to Globus (opens the browser unless --no-browser)\n"
        "logout        forget the Globus sign-in on this computer\n"
        "create-index  make a trial index in Globus Search and save its id in the settings\n"
        "query         run one search request (--request) and print the result\n"
        "use-globus    let Globus Search answer searches from now on\n"
        "use-local     let this computer's copy of the index answer (the default)",
        ["pcldb search status",
         "pcldb search query --request \"{\\\"q\\\": \\\"*\\\", \\\"limit\\\": 5}\"",
         "pcldb search query --request @request.json"])
    search.add_argument("action", choices=SEARCH_ACTIONS, help="what to do")
    search.add_argument("--no-browser", dest="no_browser", action="store_true",
                        help="for login: print the address instead of opening the browser")
    search.add_argument("--name", dest="name", default=None, metavar="TEXT",
                        help="for create-index: the index's name (anyone who knows the index "
                             "id can read it)")
    search.add_argument("--description", dest="description", default=None, metavar="TEXT",
                        help="for create-index: the index's description")
    search.add_argument("--request", dest="request", default=None, metavar="JSON",
                        help="for query: the request as JSON text, or @ and a file name")
    search.add_argument("--wait", dest="wait_seconds", type=_seconds, default=None,
                        metavar="SECONDS",
                        help="for push: how long to wait for Globus Search to finish "
                             "(default 60)")

    # -- ask ----------------------------------------------------------------------------------
    ask = add(
        "ask", "ask a question in plain English (normal mode)",
        "The ARC assistant turns the question into a search and writes a short answer. When\n"
        "it cannot be reached (it only answers on the VT network or VPN), keyword rules plan\n"
        "the search instead and the output says so.",
        ["pcldb ask \"which sessions from last week have adenine?\"",
         "pcldb ask --no-llm --json \"how many sessions per instrument\""])
    ask.add_argument("question", metavar="QUESTION", help="the question, in quotes")
    ask.add_argument("--no-llm", dest="no_llm", action="store_true",
                     help="do not use the ARC assistant: plan with the keyword rules")
    ask.add_argument("--json", dest="as_json", action="store_true",
                     help="print the whole result as JSON")
    ask.add_argument("--no-simulated", dest="no_simulated", action="store_true",
                     help="leave simulated sessions out")

    # -- sql ----------------------------------------------------------------------------------
    sql = add(
        "sql", "run one read-only SQL query (expert mode)",
        "The query runs in the same sandbox as the website's expert view: it can read the\n"
        "catalogue and change nothing.",
        ["pcldb sql \"SELECT instrument, COUNT(*) FROM sessions GROUP BY instrument\"",
         "pcldb sql --csv \"SELECT path, started_utc FROM sessions\""])
    sql.add_argument("sql", metavar="SQL", help="the query, in quotes")
    sql.add_argument("--csv", dest="as_csv", action="store_true", help="print the rows as CSV")
    sql.add_argument("--json", dest="as_json", action="store_true",
                     help="print the result as JSON")
    sql.add_argument("--max-rows", dest="max_rows", type=_row_count, default=None, metavar="N",
                     help="stop after N rows (default: the setting sql.max_rows)")

    return parser


# ---------------------------------------------------------------------- running a command


def command_function(command):
    """The function that does a command's work, or None while that part is not built yet."""
    module_name, function_name = COMMANDS[command]
    full_name = "%s.%s" % (__package__, module_name)
    try:
        module = importlib.import_module(full_name)
    except ModuleNotFoundError as error:
        if error.name == full_name:
            return None
        raise       # the part is there but something it needs is not: that must be seen
    return getattr(module, function_name, None)


def dispatch(args, settings):
    """Run the command that `args` names. Returns its exit code."""
    function = command_function(args.command)
    if function is None:
        _problem("The command \"pcldb %s\" is not built yet in this copy of pcldb (its part, "
                 "pcldb/%s.py with %s, is missing)."
                 % (args.command, COMMANDS[args.command][0], COMMANDS[args.command][1]))
        return EXIT_PROBLEM
    result = function(args, settings)
    return EXIT_OK if result is None else int(result)


def main(argv=None, *, settings=None):
    """`python -m pcldb ...`: returns the exit code (0 done, 1 a problem, 2 wrong usage).

    `settings` is for tests and for code that has its Settings already: when it is given, the
    settings file and the environment are not read (and `--dir` is ignored).
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:      # --help, --version (0) or wrong usage (2)
        code = stop.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else EXIT_USAGE)
    if not args.command:
        parser.print_usage(sys.stderr)
        _problem("Say which command to run. The list: pcldb --help")
        return EXIT_USAGE
    try:
        if settings is None:
            from . import config
            settings = config.load_settings(project_dir=args.dir)
        return dispatch(args, settings)
    except PclError as error:
        _problem(error.message)
        return EXIT_PROBLEM
    except KeyboardInterrupt:
        _problem("Stopped.")
        return 130
    except BrokenPipeError:
        # The program that read our output went away (for example: pcldb sql ... | head).
        try:
            sys.stdout = open(os.devnull, "w", encoding="ascii")
        except OSError:
            pass
        return EXIT_PROBLEM


# ---------------------------------------------------------------------- setup


def admin_token_path(settings):
    """var/admin_token.txt: the local admin's token, one line, private."""
    return settings.var_dir / ADMIN_TOKEN_FILE


def _is_local_postgres(settings):
    return (settings.database.engine == "postgresql"
            and settings.database.postgresql.mode == "local")


def _engine_word(engine):
    return "PostgreSQL" if engine == "postgresql" else "SQLite"


_SWITCH = ("To use %s instead: change \"database.engine\" in pcldb_config.json to \"%s\" and run "
           "pcldb setup again.")
_INSTALL_POSTGRES = ("Mac: brew install postgresql@18; Windows: winget install "
                     "PostgreSQL.PostgreSQL.18")


def choose_engine(settings, wanted=None, env=None):
    """(engine, sentences) for a project folder that has no settings file yet.

    `wanted` is --engine. Without it: the variable PCLDB_ENGINE; else PostgreSQL when its
    programs and the Python package psycopg are both on this computer; else SQLite. The
    sentences say what was chosen, why, and how to choose the other.
    """
    from . import util
    env = os.environ if env is None else env
    if wanted:
        return wanted, ["Engine: %s (chosen with --engine)." % _engine_word(wanted)]
    from_env = (env.get("PCLDB_ENGINE") or "").strip()
    if from_env:
        engine = settings.database.engine
        return engine, ["Engine: %s (the variable PCLDB_ENGINE says so)." % _engine_word(engine)]
    from .store import pgcluster
    found = pgcluster.find_pg_bins(settings.database.postgresql.bin_dir)
    has_driver = importlib.util.find_spec("psycopg") is not None
    if found and has_driver:
        return "postgresql", [
            "Engine: PostgreSQL %s. Its programs were found in %s."
            % (found[0].version, found[0].bin_dir),
            _SWITCH % ("SQLite (two plain files, nothing to install)", "sqlite")]
    if found:
        why = ("PostgreSQL's programs were found, but the Python package psycopg is not "
               "installed (%s installs it)" % util.INSTALL_PY)
    else:
        why = "PostgreSQL's programs were not found on this computer"
    return "sqlite", [
        "Engine: SQLite (two plain files, nothing to install), because %s." % why,
        "To use PostgreSQL instead: install it (%s), then change \"database.engine\" in "
        "pcldb_config.json to \"postgresql\" and run pcldb setup again." % _INSTALL_POSTGRES]


def _check_before_first_setup(settings, engine, port):
    """Before a settings file is written: refuse what would fail right after, so that a first
    setup that cannot work leaves nothing behind and can simply be run again with other
    options."""
    changes = {"database.engine": engine}
    if port is not None:
        changes["database.postgresql.port"] = port
    candidate = settings.changed(changes)
    if not _is_local_postgres(candidate):
        return
    from . import util
    from .store import pgcluster, postgres_engine
    postgres_engine.load_psycopg(settings)  # the project's own settings file decides
    pgcluster.choose_pg_bin(candidate)
    cluster = pgcluster.LocalCluster(candidate)
    if not cluster.is_initialized() and util.port_in_use(cluster.port):
        raise PclError(
            "Port %d on this computer is already in use, so the local PostgreSQL database "
            "cannot start there. Choose a free port below 49152, for example: pcldb setup "
            "--port %d   Or use SQLite, which needs no port: pcldb setup --engine sqlite   "
            "Nothing was written." % (cluster.port, cluster.port + 1), code="port_in_use",
            status=503)


def _settings_for_setup(args, settings):
    """The settings setup works with; writes pcldb_config.json when there is none."""
    from . import config, util
    wanted_engine = getattr(args, "engine", None)
    wanted_port = getattr(args, "port", None)
    path = settings.config_path
    from_env = (os.environ.get("PCLDB_ENGINE") or "").strip()
    if wanted_engine and from_env and from_env != wanted_engine:
        raise PclError(
            "--engine says %s, but the variable PCLDB_ENGINE says %s, and the variable wins. "
            "Unset PCLDB_ENGINE (or leave --engine out) and run pcldb setup again. Nothing was "
            "changed." % (wanted_engine, from_env), code="engine_conflict")
    if path.exists():
        _say("Settings file: %s (it exists already and was not changed)." % path)
        engine = settings.database.engine
        if wanted_engine or from_env:
            _say("Engine: %s." % _engine_word(engine))
        else:
            other = "sqlite" if engine == "postgresql" else "postgresql"
            _say("Engine: %s, as that file says (without --engine, setup keeps the engine of a "
                 "settings file that exists). To switch to %s: %s dump FILE, change "
                 "\"database.engine\" in that file to \"%s\", %s setup, then %s restore "
                 "FILE --include-users." % (_engine_word(engine), _engine_word(other),
                                            util.LAUNCHER, other, util.LAUNCHER,
                                            util.LAUNCHER))
    else:
        engine, sentences = choose_engine(settings, wanted_engine)
        _check_before_first_setup(settings, engine, wanted_port)
        content = {
            "_about": "Settings of this pcldb folder, written by pcldb setup. Every setting "
                      "and its default is explained in %s. A key that is left out keeps its "
                      "default. Passwords, tokens and keys do not belong in this file."
                      % config.EXAMPLE_NAME,
            "database": {"engine": engine},
        }
        if wanted_port is not None:
            content["database"]["postgresql"] = {"port": wanted_port}
        util.write_json_atomic(path, content)
        _say("Settings file: %s (written now)." % path)
        for sentence in sentences:
            _say(sentence)
        settings = config.load_settings(project_dir=settings.project_dir,
                                        user_dir=settings.user_dir)
    engine = settings.database.engine
    if wanted_engine and wanted_engine != engine:
        raise PclError(
            "--engine says %s, but %s says %s. pcldb setup never changes a settings file that "
            "exists. To switch: change \"database.engine\" in that file to \"%s\" and run %s "
            "setup again. (To carry the records over: %s dump FILE before, %s restore "
            "FILE --include-users after.)" % (wanted_engine, path, engine, wanted_engine,
                                              util.LAUNCHER, util.LAUNCHER, util.LAUNCHER),
            code="engine_conflict")
    if wanted_port is not None and wanted_port != settings.database.postgresql.port:
        raise PclError(
            "--port says %d, but %s says %d for the local PostgreSQL database. pcldb setup "
            "never changes a settings file that exists. Change \"database.postgresql.port\" "
            "in that file yourself, then run pcldb setup again."
            % (wanted_port, path, settings.database.postgresql.port), code="port_conflict")
    if wanted_port is not None and not _is_local_postgres(settings):
        _say("Note: --port is the port of the local PostgreSQL database; this setup does not "
             "use one, so the port is only kept in the settings file.")
    return settings


def ensure_admin(store, settings):
    """Make the user `admin` and save its token, but only when there is no user at all.
    True when it was made now. The token is never printed."""
    from . import auth
    path = admin_token_path(settings)
    written = False
    try:
        with store.app() as db:
            if auth.count_users(db) > 0:
                return False
            auth.create_user(db, ADMIN_NAME, "admin", ADMIN_DISPLAY_NAME)
            token = auth.issue_token(db, ADMIN_NAME, label="made by pcldb setup")
            auth.save_token_file(path, token)
            written = True
    except BaseException:
        if written:     # the user was not committed, so the saved token is worth nothing
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return True


def _say_admin(made, store, settings):
    path = admin_token_path(settings)
    if made:
        _say("Made the user \"%s\" (role admin)." % ADMIN_NAME)
        _say("Its token is saved in: %s" % path)
        _say("The token is not shown here. pcldb's own commands read it from that file, and "
             "pcldb serve signs the browser in by itself. Keep the file private.")
        return
    from . import auth
    with store.app() as db:
        count = auth.count_users(db)
    _say("Users: %d exist already; none was added." % count)
    if path.is_file():
        _say("The admin's token file: %s" % path)
    else:
        _say("The admin's token file %s is not there. Make a new token with: pcldb token new "
             "--user %s" % (path, ADMIN_NAME))


def _load_demo(store, settings):
    """`setup --demo`: load the demo data through demo.load_demo. Returns an exit code."""
    import datetime
    try:
        demo = importlib.import_module(__package__ + ".demo")
    except ModuleNotFoundError as error:
        if error.name != __package__ + ".demo":
            raise
        demo = None
    load_demo = getattr(demo, "load_demo", None)
    if load_demo is None:
        _say("The demo data was not loaded: that part (pcldb/demo.py) is not built yet in this "
             "copy of pcldb. Everything else is set up.")
        return EXIT_OK
    with store.app() as db:
        if db.scalar("SELECT value FROM {pcl}.meta WHERE key = ?", ["demo"]) == "1":
            _say("The demo data is loaded already; nothing was added.")
            return EXIT_OK
        result = load_demo(db, settings, datetime.datetime.now(datetime.timezone.utc))
    count = result.get("sessions") if isinstance(result, dict) else None
    if isinstance(count, int) and not isinstance(count, bool):
        _say("Loaded the demo data: %d made-up sessions. Remove them with: pcldb demo clear"
             % count)
    else:
        _say("Loaded the demo data (made-up sessions). Remove them with: pcldb demo clear")
    return EXIT_OK


def _prepare_local_cluster(settings):
    """Create and start the local PostgreSQL cluster; say what happened."""
    from .store import pgcluster, postgres_engine
    postgres_engine.load_psycopg(settings)  # fail before anything is created when it is missing
    cluster = pgcluster.LocalCluster(settings)
    created = cluster.init()
    for notice in cluster.notices:
        _say("Note: " + notice)
    started = cluster.ensure_running()
    if created:
        _say("Made the local PostgreSQL database in %s." % cluster.data_dir)
    if started:
        _say("Started it on 127.0.0.1, port %d." % cluster.port)
    else:
        _say("The local PostgreSQL database is running already (127.0.0.1, port %d)."
             % cluster.port)
    return cluster


def cmd_setup(args, settings):
    """`pcldb setup [--engine E] [--demo] [--port N]`."""
    from .store import open_store
    settings = _settings_for_setup(args, settings)
    local_postgres = _is_local_postgres(settings)
    if local_postgres:
        _prepare_local_cluster(settings)
    store = open_store(settings)
    was_ready = store.is_initialized()
    store.initialize()
    where = store.describe()["where"]
    if was_ready:
        _say("The database is set up already (%s)." % where)
    else:
        _say("Set up the database: tables and views made (%s)." % where)
    _say_admin(ensure_admin(store, settings), store, settings)
    code = EXIT_OK
    if getattr(args, "demo", False):
        code = _load_demo(store, settings)
    if local_postgres:
        _say("The local PostgreSQL database keeps running. Stop it with: pcldb db stop")
    from . import util
    _say("Setup is done. Next: %s serve --open   (the website)   or: %s doctor   "
         "(a check of everything)" % (util.LAUNCHER, util.LAUNCHER))
    return code


# ---------------------------------------------------------------------- db


def _size_text(path):
    try:
        size = path.stat().st_size
    except OSError:
        return "not there yet"
    return "%d bytes" % size


def _yes_no(value):
    return "yes" if value else "no"


def _db_status(settings):
    from .store import open_store
    store = open_store(settings)
    if store.engine == "sqlite":
        _say("Engine: SQLite (two files; nothing runs in the background).")
        _say("Folder: %s" % store.folder)
        _say("Private file (users, tokens, logs): %s  (%s)" % (store.app_path.name,
                                                             _size_text(store.app_path)))
        _say("Catalogue file (what expert mode reads): %s  (%s)"
             % (store.catalog_path.name, _size_text(store.catalog_path)))
        ready = store.is_initialized()
        _say("Set up: %s" % _yes_no(ready))
        if not ready:
            _say("Run: pcldb setup")
        return EXIT_OK
    if not _is_local_postgres(settings):
        described = store.describe()
        _say("Engine: PostgreSQL on a server that pcldb does not start or stop (%s)."
             % described["where"])
        try:
            ready = store.is_initialized()
        except PclError as error:
            _say("It does not answer: %s" % error.message)
            return EXIT_OK
        _say("Version: %s" % store.describe()["version"])
        _say("Set up: %s" % _yes_no(ready))
        if not ready:
            _say("Run: pcldb setup")
        return EXIT_OK
    from .store import pgcluster
    status = pgcluster.LocalCluster(settings).status()
    _say("Engine: PostgreSQL, kept inside this project folder.")
    _say("Data folder: %s" % status["data_dir"])
    if status["bin_dir"]:
        _say("PostgreSQL's programs: %s (version %s)" % (status["bin_dir"], status["version"]))
    else:
        _say("PostgreSQL's programs: not found. Install PostgreSQL (%s) or set "
             "database.postgresql.bin_dir." % _INSTALL_POSTGRES)
    _say("Created: %s" % _yes_no(status["initialized"]))
    if not status["initialized"]:
        _say("Running: no")
        _say("Run: pcldb setup")
        return EXIT_OK
    if status["running"]:
        _say("Running: yes (127.0.0.1, port %s, process %s)" % (status["port"], status["pid"]))
        try:
            ready = store.is_initialized()
        except PclError as error:
            _say("Set up: unknown. %s" % error.message)
            return EXIT_OK
        _say("Set up: %s" % _yes_no(ready))
        if not ready:
            _say("Run: pcldb setup")
    else:
        _say("Running: no (it would use port %s). Start it with: pcldb db start"
             % status["port"])
    return EXIT_OK


def _db_start_stop(settings, action):
    if settings.database.engine == "sqlite":
        _say("The SQLite engine has nothing to %s: its two files are opened when they are "
             "needed." % action)
        return EXIT_OK
    if not _is_local_postgres(settings):
        _say("This project uses a PostgreSQL server that pcldb does not start or stop. "
             "Nothing was done.")
        return EXIT_OK
    from .store import pgcluster
    cluster = pgcluster.LocalCluster(settings)
    if action == "start":
        if cluster.ensure_running():
            _say("Started the local PostgreSQL database (127.0.0.1, port %d)." % cluster.port)
        else:
            _say("The local PostgreSQL database is running already (127.0.0.1, port %d)."
                 % cluster.port)
    elif cluster.stop():
        _say("Stopped the local PostgreSQL database.")
    else:
        _say("The local PostgreSQL database is not running. Nothing was done.")
    return EXIT_OK


def _remove_saved_tokens(settings):
    """After a reset every saved token is dead: remove the files that held them."""
    from . import auth
    paths = [admin_token_path(settings)]
    folder = settings.var_dir / auth.TOKEN_FOLDER
    try:
        paths += [path for path in folder.iterdir() if path.suffix == ".txt" and path.is_file()]
    except OSError:
        pass
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _db_reset(settings, yes):
    from .store import open_store
    engine = _engine_word(settings.database.engine)
    if not yes:
        raise PclError(
            "pcldb db reset deletes every record, every user and every token in this project's "
            "%s database and starts it empty. The files on ARC are not touched. Nothing was "
            "deleted. To go ahead, run: pcldb db reset --yes" % engine, code="confirm_needed")
    if not settings.config_path.exists():
        raise PclError("This project folder (%s) has no settings file yet, so there is nothing "
                       "to reset. Run: pcldb setup" % settings.project_dir, code="not_set_up")
    store = open_store(settings)
    if _is_local_postgres(settings):
        from .store import pgcluster, postgres_engine
        postgres_engine.load_psycopg(settings)
        pgcluster.choose_pg_bin(settings)       # fail now, not after the data is gone
        cluster = pgcluster.LocalCluster(settings)
        cluster.destroy()
        try:
            settings.local_secrets_path.unlink()    # new database, new passwords
        except OSError:
            pass
        _say("Deleted the local PostgreSQL database (%s)." % cluster.data_dir)
        _prepare_local_cluster(settings)
        store.initialize()
    else:
        store.reset()
    _remove_saved_tokens(settings)
    _say("The %s database is empty and set up again (%s)." % (engine, store.describe()["where"]))
    _say_admin(ensure_admin(store, settings), store, settings)
    return EXIT_OK


def cmd_db(args, settings):
    """`pcldb db status|start|stop|reset [--yes]`."""
    action = getattr(args, "action", None)
    if action == "status":
        return _db_status(settings)
    if action in ("start", "stop"):
        return _db_start_stop(settings, action)
    if action == "reset":
        return _db_reset(settings, bool(getattr(args, "yes", False)))
    raise PclError("pcldb db does not know the action %r. The actions are: status, start, stop, "
                   "reset." % (action,), code="usage")
