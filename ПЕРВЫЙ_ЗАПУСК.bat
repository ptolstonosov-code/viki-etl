@echo off
title LLM ETL - install (run as Administrator)
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "setup\install.ps1"
echo.
pause
