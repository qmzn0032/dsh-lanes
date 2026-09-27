@echo off
rem ============================================================
rem  DSH multi-version launcher - build standalone exe folders
rem  Pure ASCII + CRLF on purpose: avoids cmd.exe encoding issues.
rem
rem  Output (in dist\):
rem    dist\dsh-lanes\dsh-lanes.exe            CLI   (console)
rem    dist\dsh-lanes-gui\dsh-lanes-gui.exe    GUI   (windowed)
rem
rem  IMPORTANT - this builds --onedir, NOT --onefile:
rem  --onefile unpacks itself into %TEMP% on every start. On this machine
rem  that gets blocked ("[PYI-xxxx:ERROR] Could not create temporary
rem  directory!", or "Failed to extract VCRUNTIME140.dll"), and the
rem  --windowed GUI only shows a dialog titled "Error". --onedir needs no
rem  unpacking and starts fine, so it is the default here.
rem
rem  When sharing: zip the WHOLE folder, do not copy the .exe alone -
rem  the .exe needs the DLLs and the _internal folder next to it.
rem
rem  Needs PyInstaller (once):  py -m pip install pyinstaller
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

echo === [1/2] building CLI: dist\dsh-lanes\dsh-lanes.exe ===
%PY% -m PyInstaller --noconfirm --clean --onedir --console --name dsh-lanes dsh_lanes.py
if not %errorlevel%==0 goto fail

echo === [2/2] building GUI: dist\dsh-lanes-gui\dsh-lanes-gui.exe ===
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed --name dsh-lanes-gui dsh_lanes_gui.py
if not %errorlevel%==0 goto fail

echo.
echo [OK] build finished. Run these:
echo      dist\dsh-lanes\dsh-lanes.exe doctor
echo      dist\dsh-lanes-gui\dsh-lanes-gui.exe
echo.
echo Reminder: unsigned exe - SmartScreen may ask for confirmation
echo on first run ("More info" -^> "Run anyway").
pause
exit /b 0

:fail
echo.
echo [XX] build failed - see the messages above.
pause
exit /b 1
