@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo Install Python 3.12 and retry setup_monetizer.bat.
        pause
        exit /b 1
    )
)
set "MONETIZER_REQUIREMENTS=requirements.txt"
if exist requirements.lock.txt set "MONETIZER_REQUIREMENTS=requirements.lock.txt"
".venv\Scripts\python.exe" -m pip install -r "%MONETIZER_REQUIREMENTS%"
if errorlevel 1 (
    pause
    exit /b 1
)
echo Environment is ready. Run run_monetizer.bat when you need the panel.
