#!/usr/bin/env python3
"""Make this folder's Python environment (.venv) and install what pcldb needs.

    python3 install.py            macOS and Linux
    py -3 install.py              Windows
    python3 install.py --globus   also install the package for the live Globus Search service

It only uses Python's standard library, and it is safe to run again: an environment that is
there is kept, and the packages are checked once more. Nothing outside this folder is changed.

pcldb itself needs Python 3.11 or newer. This file is written so that an older Python can still
read it and say so in a sentence (no newer syntax here, on purpose).
"""

import argparse
import os
import subprocess
import sys

MINIMUM = (3, 11)
VENV_NAME = ".venv"
REQUIREMENTS = "requirements.txt"
REQUIREMENTS_GLOBUS = "requirements-globus.txt"


def project_dir():
    """The folder this file is in."""
    return os.path.dirname(os.path.abspath(__file__))


def system_name(platform=None):
    """"windows" or "posix" (macOS, Linux)."""
    platform = sys.platform if platform is None else platform
    return "windows" if platform.startswith("win") else "posix"


def version_text(version_info=None):
    version_info = sys.version_info if version_info is None else version_info
    return ".".join(str(part) for part in tuple(version_info)[:3])


def version_problem(version_info=None):
    """None when this Python is new enough, else a sentence that names the version found."""
    version_info = sys.version_info if version_info is None else version_info
    if tuple(version_info)[:2] >= MINIMUM:
        return None
    return ("This is Python %s. pcldb needs Python %d.%d or newer. Install a newer Python (Mac: "
            "brew install python@3.13; Windows: winget install Python.Python.3.13), then run "
            "install.py with it." % (version_text(version_info), MINIMUM[0], MINIMUM[1]))


def venv_dir(project):
    return os.path.join(project, VENV_NAME)


def venv_python(project, system):
    """Where the environment's own Python is."""
    if system == "windows":
        return os.path.join(venv_dir(project), "Scripts", "python.exe")
    return os.path.join(venv_dir(project), "bin", "python")


def requirement_files(project, globus):
    """The requirement files to install, in order."""
    names = [REQUIREMENTS]
    if globus:
        names.append(REQUIREMENTS_GLOBUS)
    return [os.path.join(project, name) for name in names]


def plan(project, python, system, globus, venv_exists):
    """The steps as (what it does, the command as a list). No shell is ever used."""
    steps = []
    if not venv_exists:
        steps.append(("Making the Python environment in %s" % venv_dir(project),
                      [python, "-m", "venv", venv_dir(project)]))
    command = [venv_python(project, system), "-m", "pip", "install",
               "--disable-pip-version-check"]
    for path in requirement_files(project, globus):
        command += ["-r", path]
    what = "Installing the packages (psycopg%s)" % (" and globus-sdk" if globus else "")
    steps.append((what, command))
    return steps


def next_commands(system):
    """The two commands to type next, for this system."""
    if system == "windows":
        return [".\\pcldb.cmd setup", ".\\pcldb.cmd serve --open"]
    return ["./pcldb.sh setup", "./pcldb.sh serve --open"]


def _run(command):
    """Run one step; its exit code, or None when the program could not be started."""
    try:
        return subprocess.call(command)
    except OSError:
        return None


def main(argv=None, run=None, out=None, version_info=None, platform=None, project=None,
         python=None):
    """Returns the exit code. The keyword arguments are for tests."""
    out = sys.stdout if out is None else out

    def say(text):
        out.write(text + "\n")

    problem = version_problem(version_info)
    if problem:
        say(problem)
        return 1
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Make this folder's Python environment (.venv) and install what pcldb "
                    "needs. Safe to run again.")
    parser.add_argument("--globus", action="store_true",
                        help="also install globus-sdk (only needed for the live Globus Search "
                             "service; the local search needs nothing)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 2)

    run = _run if run is None else run
    project = project_dir() if project is None else project
    system = system_name(platform)
    python = (sys.executable or "python3") if python is None else python
    missing = [path for path in requirement_files(project, args.globus)
               if not os.path.isfile(path)]
    if missing:
        say("%s is missing. install.py must stay in the pcldb project folder, beside "
            "requirements.txt." % missing[0])
        return 1

    venv_exists = os.path.isfile(venv_python(project, system))
    if venv_exists:
        say("The Python environment %s is there already; it is kept." % venv_dir(project))
    for what, command in plan(project, python, system, args.globus, venv_exists):
        say(what + " ...")
        code = run(command)
        if code == 0:
            continue
        if command[1:3] == ["-m", "venv"]:
            say("The Python environment could not be made (Python's venv module ended with %s). "
                "On Debian or Ubuntu install it first: sudo apt install python3-venv" % code)
        else:
            say("The packages could not be installed (pip ended with %s). Check that this "
                "computer can reach the internet (pypi.org), then run install.py again." % code)
        return 1

    first, second = next_commands(system)
    say("Done. pcldb is installed in %s (Python %s)." % (venv_dir(project),
                                                        version_text(version_info)))
    say("Next, in the folder %s:" % project)
    say("  %s" % first)
    say("  %s" % second)
    if not args.globus:
        say("(For the live Globus Search service, later: run install.py --globus)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
