@echo off
rem ============================================================
rem  Build the 46 KB launcher stub.
rem
rem  It is a GUI-subsystem (winexe) C# shell: it locates pythonw.exe
rem  and runs dsh_lanes_gui.py next to itself. No console window,
rem  no bundled Python, and editing the .py files needs no rebuild.
rem
rem  Output: ..\DSH多版本启动器-轻量.exe
rem  Needs: .NET Framework 4.x csc.exe (ships with Windows 10/11).
rem
rem  NOTE: this file is saved as GBK on purpose - cmd.exe reads .bat
rem  with the OEM code page (936 on a Chinese Windows), and the
rem  output file name below is Chinese.
rem ============================================================
setlocal
cd /d "%~dp0"

set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo [XX] csc.exe not found - .NET Framework 4.x is required.
    pause
    exit /b 1
)

"%CSC%" /nologo /target:winexe /optimize+ /win32icon:"..\assets\app.ico" ^
    /r:System.dll /r:System.Windows.Forms.dll /r:System.Drawing.dll ^
    /out:"..\DSH多版本启动器-轻量.exe" "dsh_stub.cs"

if exist "..\DSH多版本启动器-轻量.exe" (
    echo.
    echo [OK] built: ..\DSH多版本启动器-轻量.exe
) else (
    echo.
    echo [XX] build failed - see the messages above.
)
pause