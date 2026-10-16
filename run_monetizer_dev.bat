@echo off
setlocal
cd /d "%~dp0"
set "MONETIZER_DEV=1"
call run_monetizer.bat --dev
endlocal
