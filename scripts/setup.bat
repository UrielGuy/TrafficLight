@echo off
rem One-time (and after-update) setup on Windows: .venv, Python packages, web UI build.
rem Safe to run again: it only does what is missing or out of date.
setlocal EnableExtensions
cd /d "%~dp0.." || goto :fail
set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Creating .venv ...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create .venv. Install Python 3.11 or newer from https://www.python.org
        echo ^(tick "py launcher"^) and run this again.
        goto :fail
    )
)
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo The Python in .venv is older than 3.11. Delete the .venv folder, install a newer Python, run this again.
    goto :fail
)

echo Checking Python packages ...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo pip failed. Packages are downloaded here, so this needs an internet connection.
    goto :fail
)

rem Is web\dist missing, or older than a file under web\src or the web config files?
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = Get-Item 'web\dist\index.html' -ErrorAction SilentlyContinue; if (-not $d) { exit 1 }; $n = @(Get-ChildItem 'web\src' -Recurse -File) + @(Get-Item 'web\index.html','web\package.json','web\package-lock.json','web\vite.config.ts','web\tsconfig.json' -ErrorAction SilentlyContinue) | Sort-Object LastWriteTime | Select-Object -Last 1; if ($n -and $n.LastWriteTime -gt $d.LastWriteTime) { exit 1 } else { exit 0 }"
if not errorlevel 1 (
    echo Web UI is up to date.
    goto :done
)

where npm >nul 2>nul
if errorlevel 1 (
    echo.
    echo The web UI ^(web\dist^) is missing or older than its sources, and Node.js is not installed.
    echo Install Node.js ^(https://nodejs.org^) and run this again, or copy a built web\dist from
    echo another PC. The server still starts without it, but shows only a placeholder page.
    goto :done
)

echo Building the web UI ...
pushd web || goto :fail
if not exist node_modules (
    call npm ci --no-audit --no-fund
    if errorlevel 1 popd & goto :fail
)
call npm run build
if errorlevel 1 popd & goto :fail
popd

:done
echo.
echo Setup done. Start the server with scripts\run.bat, then open http://localhost:8080
call :maybepause
exit /b 0

:fail
echo.
echo Setup failed.
call :maybepause
exit /b 1

:maybepause
rem Keep the window open when started by double-click. TRAFFICLIGHT_NOPAUSE=1 disables this.
if defined TRAFFICLIGHT_NOPAUSE exit /b 0
echo %cmdcmdline% | find /i "/c" >nul && pause
exit /b 0
