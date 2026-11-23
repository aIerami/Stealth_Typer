@echo off
rem Starts Microsoft Edge with a remote debugging port (for BROWSER_MODE = "attach").
rem A separate profile folder (edge_debug_profile) is used: log in to your sites once in this window.
rem NOTE: keep this file ASCII-only (no Cyrillic), otherwise cmd.exe breaks.

set PORT=9222
set PROFILE=%~dp0edge_debug_profile
set EDGE=

if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%LocalAppData%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%LocalAppData%\Microsoft\Edge\Application\msedge.exe"

if not defined EDGE goto noedge

echo Starting Edge with debugging port %PORT% ...
start "" "%EDGE%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run
echo.
echo Done. Open your page in the new Edge window, then run run.bat
timeout /t 5 >nul
goto :eof

:noedge
echo Microsoft Edge was not found.
echo Open this file in Notepad and set the path to msedge.exe in the EDGE variable.
pause
