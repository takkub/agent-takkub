@echo off
REM scripts\run.bat — thin wrapper. The real launcher lives at the repo root.
REM Kept for backward compatibility with existing docs / shortcuts.

setlocal
set HERE=%~dp0
call "%HERE%..\agent-takkub.bat"
endlocal
