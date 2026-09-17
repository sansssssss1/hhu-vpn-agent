@echo off
rem Keep ASCII-only (see note in open-databases.cmd).
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-guard.ps1" %*
echo.
pause
