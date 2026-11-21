@echo off
rem pcldb: runs the command line of this folder ("pcldb --help" lists the commands).
rem It uses the Python environment in .venv when there is one (py -3 install.py makes it),
rem else the Python launcher "py -3". Works from any folder.
setlocal
set "PCLDB_HERE=%~dp0"
if "%PCLDB_HERE:~-1%"=="\" set "PCLDB_HERE=%PCLDB_HERE:~0,-1%"
if defined PYTHONPATH goto addpath
set "PYTHONPATH=%PCLDB_HERE%"
goto choose
:addpath
set "PYTHONPATH=%PCLDB_HERE%;%PYTHONPATH%"
:choose
if exist "%PCLDB_HERE%\.venv\Scripts\python.exe" goto venv
where py >nul 2>nul
if errorlevel 1 goto nopython
py -3 -m pcldb %*
exit /b %ERRORLEVEL%
:venv
"%PCLDB_HERE%\.venv\Scripts\python.exe" -m pcldb %*
exit /b %ERRORLEVEL%
:nopython
echo pcldb: Python was not found. Install Python 3.11 or newer from python.org, then run: py -3 install.py 1>&2
exit /b 1
