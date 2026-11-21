#!/bin/sh
# pcldb.sh: runs the command line of this folder ("./pcldb.sh --help" lists the commands).
# It uses the Python environment in .venv when there is one (python3 install.py makes it),
# else the python3 on this computer. Works from any folder.
# (It cannot be named plain "pcldb": that is the name of the folder with the Python code.)

self=$0
# When this file was reached through a link, find the folder it really is in.
while [ -h "$self" ]; do
    target=$(readlink "$self") || break
    case $target in
        /*) self=$target ;;
        *) self=$(dirname -- "$self")/$target ;;
    esac
done
here=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd -P) || {
    echo "pcldb: the folder of this script could not be found." >&2
    exit 1
}

if [ -x "$here/.venv/bin/python" ]; then
    python=$here/.venv/bin/python
elif [ -x "$here/.venv/Scripts/python.exe" ]; then
    python=$here/.venv/Scripts/python.exe
elif command -v python3 >/dev/null 2>&1; then
    python=python3
elif command -v python >/dev/null 2>&1; then
    python=python
else
    echo "pcldb: Python was not found. Install Python 3.11 or newer, then run: python3 install.py" >&2
    exit 1
fi

if [ -n "${PYTHONPATH:-}" ]; then
    PYTHONPATH=$here:$PYTHONPATH
else
    PYTHONPATH=$here
fi
export PYTHONPATH

exec "$python" -m pcldb "$@"
