@echo off
rem ============================================================
rem  DSH multi-version launcher - build standalone .exe files
rem  Pure ASCII + CRLF on purpose: avoids cmd.exe encoding issues.
rem
rem  Output (in dist\):
rem    dsh-lanes.exe        CLI   (console window)
rem    dsh-lanes-gui.exe    GUI   (windowed, double-click to run)
rem
rem  Needs PyInstaller (once):
rem    py -m pip install pyinstaller
rem
rem  Note: lanes.json is kept NEXT TO the .exe (portable - copy the
rem  exe anywhere and it carries its own config). If that folder is
rem  not writable (e.g. Program Files), it falls back to
rem  %APPDATA%\dsh-lanes automatically.
rem ============================================================
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)

%PY% -m PyInstaller --version >nul 2>nul
if not %errorlevel%==0 (
    echo [XX] PyInstaller not found. Install it first:
    echo      %PY% -m pip install pyinstaller
    pause
    exit /b 1
)

echo === [1/2] building CLI exe: dist\dsh-lanes.exe ===
%PY% -m PyInstaller --noconfirm --clean --onefile --console --name dsh-lanes dsh_lanes.py
if not %errorlevel%==0 goto fail

echo === [2/2] building GUI exe: dist\dsh-lanes-gui.exe ===
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name dsh-lanes-gui dsh_lanes_gui.py
if not %errorlevel%==0 goto fail

echo.
echo [OK] build finished. Files in dist\:
dir /b "dist\*.exe"
echo.
echo Reminder: unsigned exe - Windows SmartScreen may ask for
echo confirmation on first run ("More info" -^> "Run anyway").
pause
exit /b 0

:fail
echo.
echo [XX] build failed - see the messages above.
pause
exit /b 1
