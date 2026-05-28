@echo off
title LLM ETL - package for GPU training host
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "setup\package_for_gpu.ps1"
echo.
pause
