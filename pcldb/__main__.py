"""`python -m pcldb <command>`: the command line (see pcldb/cli.py)."""

import sys

if sys.version_info < (3, 11):
    # Said here, in syntax every Python understands, before anything newer is imported.
    sys.stderr.write(
        "pcldb needs Python 3.11 or newer; this is Python %d.%d.%d. Run install.py with a newer "
        "Python (it makes the folder .venv, which the launchers pcldb.sh and pcldb.cmd then "
        "use).\n" % tuple(sys.version_info[:3]))
    sys.exit(1)

from .cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
