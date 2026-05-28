@echo off
title LLM ETL
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] Not installed. Run PERVYY_ZAPUSK.bat first ^(as Administrator^).
    pause
    exit /b 1
)

tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        start "" /B "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
        timeout /t 3 /nobreak >nul
    )
)

start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:7860"

set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" main.py ui

pause
