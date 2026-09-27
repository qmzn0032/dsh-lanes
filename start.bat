@echo off
rem ============================================================
rem  DeepSeek Harness multi-version launcher (step 1)
rem  Pure ASCII + CRLF on purpose: avoids cmd.exe encoding issues.
rem  Usage:  start.bat open next        (any dsh_lanes.py argument)
rem ============================================================
title DSH Multi-Version Launcher
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py "dsh_lanes.py" %*
) else (
    python "dsh_lanes.py" %*
)

echo.
pause