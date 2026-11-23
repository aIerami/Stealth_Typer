@echo off

rem Runs Stealth Typer. Uses a virtual environment next to the script if there is one
rem (venv, .venv, stealth-env, env), otherwise the system Python.
rem NOTE: keep this file ASCII-only (no Cyrillic), otherwise cmd.exe breaks.

cd /d "%~dp0"

set "PY="
for %%D in (venv .venv stealth-env env) do (
  if not defined PY if exist "%%D\Scripts\python.exe" set "PY=%%D\Scripts\python.exe"
)
if not defined PY set "PY=python"

echo Using Python: %PY%
echo.
"%PY%" stealth_typer.py %*

echo.
pause
