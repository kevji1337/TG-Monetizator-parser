@echo off
chcp 65001 > nul
title Telegram Monetizer
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call setup_monetizer.bat
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -c "import importlib.util as u; raise SystemExit(any(u.find_spec(n) is None for n in ('streamlit','openpyxl','bs4','requests','selenium','undetected_chromedriver')))" >nul 2>&1
if errorlevel 1 (
    call setup_monetizer.bat
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" launch_monetizer.py %*
if errorlevel 1 pause
