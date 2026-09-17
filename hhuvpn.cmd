@echo off
rem hhuvpn 命令行包装（Windows）。把本目录加入 PATH 后即可全局使用 hhuvpn。
setlocal
set "HHUVPN_DIR=%~dp0"
python "%HHUVPN_DIR%hhuvpn.py" %*
exit /b %ERRORLEVEL%
