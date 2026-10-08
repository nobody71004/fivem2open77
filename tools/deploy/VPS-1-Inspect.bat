@echo off
REM Read-only look at both XBUNIVERSE servers on the VPS (build, players online, requiredMods,
REM what is installed).  Log: dist\vps-inspect.log
setlocal
set "HERE=%~dp0"
set "BUNDLE=%HERE%..\..\dist\i8rpncs-bundle.tgz"
set "LOGDIR=%HERE%..\..\dist"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Deploy-Car.ps1" -Bundle "%BUNDLE%" -Mode inspect -Log "%LOGDIR%\vps-inspect.log"
echo.
echo Exit code %ERRORLEVEL% - full log: %LOGDIR%\vps-inspect.log
pause
