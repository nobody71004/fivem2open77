@echo off
REM Install the converted cars on both XBUNIVERSE servers on the VPS and restart each one that has
REM nobody online (others are left pending).  Backups in /opt/open77-backups.  Log: dist\vps-deploy.log
setlocal
set "HERE=%~dp0"
set "BUNDLE=%HERE%..\..\dist\i8rpncs-bundle.tgz"
set "LOGDIR=%HERE%..\..\dist"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Deploy-Car.ps1" -Bundle "%BUNDLE%" -Mode deploy -Restart -Log "%LOGDIR%\vps-deploy.log"
echo.
echo Exit code %ERRORLEVEL% - full log: %LOGDIR%\vps-deploy.log
pause
