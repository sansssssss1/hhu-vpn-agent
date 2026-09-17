@echo off
rem ---------------------------------------------------------------------------
rem  Standalone launcher (no agent needed): ensure campus network + school WebVPN,
rem  then open the target literature database in the default browser.
rem
rem  Keep this file ASCII-only: cmd.exe parses UTF-8 batch files unreliably,
rem  especially for multibyte text inside "( )" blocks. Chinese guidance lives in
rem  the Python CLI output and in standalone/README.md.
rem
rem  Usage (double-click works too):
rem     open-databases.cmd               -> databases from config.ini [databases] default
rem     open-databases.cmd cnki sd       -> specific databases
rem     open-databases.cmd --portal      -> just open the WebVPN portal
rem ---------------------------------------------------------------------------
chcp 65001 >nul
setlocal
set "PYTHONIOENCODING=utf-8"
for %%I in ("%~dp0..") do set "PROJ=%%~fI"

echo.
echo === HHU campus network + WebVPN : login and open databases ===
echo project : %PROJ%
echo.
python "%PROJ%\hhuvpn.py" open %*
set "RC=%ERRORLEVEL%"
echo.
echo exit code: %RC%
if not "%RC%"=="0" echo  hint: run  python "%PROJ%\hhuvpn.py" doctor   for details
if "%RC%"=="4" echo  hint: run  python "%PROJ%\hhuvpn.py" config set-password
if "%RC%"=="3" echo  hint: run  python "%PROJ%\hhuvpn.py" login webvpn --browser
echo.
pause
