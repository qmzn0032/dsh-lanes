@echo off
rem ============================================================
rem  DSH multi-version launcher - GUI (application window)
rem  Pure ASCII + CRLF on purpose: avoids cmd.exe encoding issues.
rem ============================================================
title DSH Multi-Version Launcher
cd /d "%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "dsh_lanes_gui.py"
    exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "dsh_lanes_gui.py"
    exit /b
)

rem Fallback: run with a console so errors stay visible
py "dsh_lanes_gui.py"