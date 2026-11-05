@echo off
rem Start the TrafficLight server. Arguments are passed on, e.g.
rem   scripts\run.bat --port 8090 --data D:\trafficlight-data
rem Ctrl+C stops it. Opens no browser: go to http://localhost:8080
setlocal EnableExtensions
cd /d "%~dp0.." || exit /b 1

if not exist ".venv\Scripts\python.exe" (
    echo No .venv yet. Run scripts\setup.bat first.
    set "RC=1"
    goto :end
)
if not exist "web\dist\index.html" (
    echo Note: web\dist is missing, so the server will show only a placeholder page. Run scripts\setup.bat.
)

".venv\Scripts\python.exe" -m trafficlight %*
set "RC=%errorlevel%"

:end
rem Keep the window open on failure when started by double-click.
if not "%RC%"=="0" if not defined TRAFFICLIGHT_NOPAUSE echo %cmdcmdline% | find /i "/c" >nul && pause
exit /b %RC%
