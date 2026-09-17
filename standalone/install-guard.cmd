@echo off
rem Keep ASCII-only (see note in open-databases.cmd).
rem Registers a scheduled task: campus network relogin + WebVPN keepalive.
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-guard.ps1" %*
echo.
pause
