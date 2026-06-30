@echo off
REM ===================================================================
REM  fix-qt-lts.bat  —  swap the cockpit's Qt 6.11 for the 6.8 LTS series
REM
REM  Qt 6.11.0 ships a Qt6Core.dll regression that hard-crashes the cockpit
REM  with 0xc0000409 (__fastfail) — no Python traceback, the window just
REM  vanishes ("ดับ"). 6.8 is the battle-tested LTS.
REM
REM  Qt DLLs can't be replaced while the cockpit is running, so this script
REM  closes it first, downgrades, then relaunches on the new version.
REM
REM  HOW TO RUN: double-click this file in Windows Explorer (NOT from inside
REM  a cockpit pane — it closes the cockpit, which would kill the pane mid-run).
REM ===================================================================
setlocal
cd /d "%~dp0\.."

echo.
echo [1/4] Closing any running cockpit...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | Where-Object { $_.CommandLine -match 'agent_takkub' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
REM give Windows a moment to release the Qt DLL handles
powershell -NoProfile -Command "Start-Sleep -Seconds 2"

echo.
echo [2/4] Installing PyQt6 / Qt 6.8 LTS (this can take a minute)...
".venv\Scripts\python.exe" -m pip install "PyQt6==6.8.*" "PyQt6-Qt6==6.8.*" "PyQt6-WebEngine==6.8.*" "PyQt6-WebEngine-Qt6==6.8.*"
if errorlevel 1 (
  echo.
  echo [!] pip install failed. Make sure every cockpit window is closed, then re-run.
  pause
  exit /b 1
)

echo.
echo [3/4] Verifying installed version...
".venv\Scripts\python.exe" -c "import PyQt6.QtCore as c; print('   Qt runtime now:', c.QT_VERSION_STR)"

echo.
echo [4/4] Relaunching cockpit...
start "" ".venv\Scripts\pythonw.exe" -m agent_takkub

echo.
echo Done. The cockpit should reopen on Qt 6.8 LTS.
echo (If it didn't, launch it the usual way.)
timeout /t 4 >nul
endlocal
