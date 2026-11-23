@echo off
rem Example launcher for one site. Copy this file (e.g. run_shop.bat), set SITE to a profile name
rem from config.py (SITES). Use LATIN letters only in this file and in the profile name.
rem NOTE: keep this file ASCII-only (no Cyrillic), otherwise cmd.exe breaks.

set SITE=my_site

call "%~dp0run.bat" --site %SITE% %*
