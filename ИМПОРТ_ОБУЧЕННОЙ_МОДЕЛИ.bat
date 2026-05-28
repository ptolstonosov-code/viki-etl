@echo off
title LLM ETL - import trained model
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "setup\import_trained_model.ps1"
echo.
pause
