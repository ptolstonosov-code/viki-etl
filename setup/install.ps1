# ─────────────────────────────────────────────────────────────────────────────
# LLM ETL — one-shot installer
# Run as Administrator from the project root:
#   PowerShell -ExecutionPolicy Bypass -File setup\install.ps1
#
# Steps:
#   1. Check NVIDIA GPU (CUDA)
#   2. Install Python 3.11 if missing (via winget)
#   3. Install Ollama + pull base model
#   4. Create venv and install Python deps (PyTorch w/CUDA + HF stack)
#   5. Initialise SQLite DB from schema.sql
# ─────────────────────────────────────────────────────────────────────────────

param(
    [string]$Model = "qwen2.5:7b",
    [string]$PythonVersion = "3.11",
    [switch]$SkipPyDeps
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
$Root = Split-Path -Parent $PSScriptRoot

Write-Host "`n════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  LLM ETL — первичная установка" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "Будет установлено:" -ForegroundColor White
Write-Host "  1. Движок Ollama (запускает нейросеть локально)"
Write-Host "  2. Модель qwen2.5:14b (~9 ГБ — это займёт время)"
Write-Host "  3. Python-библиотеки в локальный venv"
Write-Host "  4. База данных SQLite с 42 таблицами"
Write-Host ""
Write-Host "Папка проекта: $Root" -ForegroundColor DarkGray

# ── 1. CUDA check ─────────────────────────────────────────────────────────────
Write-Host "`n[1/5] Проверяю NVIDIA GPU…" -ForegroundColor Yellow
if (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue) {
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    Write-Host "[OK] CUDA GPU найдена." -ForegroundColor Green
} else {
    Write-Warning "nvidia-smi не найден. На этой машине разбор будет работать на CPU — обучение надо делать на отдельной GPU-машине."
}

# ── 2. Python ─────────────────────────────────────────────────────────────────
Write-Host "`n[2/5] Проверяю Python…" -ForegroundColor Yellow
$pythonCmd = Get-Command "python" -ErrorAction SilentlyContinue
if (-not $pythonCmd) { $pythonCmd = Get-Command "py" -ErrorAction SilentlyContinue }
if (-not $pythonCmd) {
    throw "Python не найден. Установите Python 3.10+ с python.org (галочка 'Add to PATH'), потом запустите этот скрипт ещё раз."
}
& $pythonCmd.Source --version
Write-Host "[OK] Python найден." -ForegroundColor Green

# ── 3. Ollama ─────────────────────────────────────────────────────────────────
Write-Host "`n[3/5] Устанавливаю Ollama…" -ForegroundColor Yellow
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (Test-Path $ollamaExe) {
    Write-Host "Ollama уже установлена."
} else {
    $installerUrl = "https://ollama.com/download/OllamaSetup.exe"
    $installerPath = "$env:TEMP\OllamaSetup.exe"
    Write-Host "Скачиваю установщик Ollama…"
    Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
    Write-Host "Запускаю установку (тихо)…"
    Start-Process -FilePath $installerPath -ArgumentList "/S" -Wait
    Write-Host "[OK] Ollama установлена." -ForegroundColor Green
}
$env:PATH += ";$env:LOCALAPPDATA\Programs\Ollama"

if (-not (Get-Process "ollama" -ErrorAction SilentlyContinue)) {
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

Write-Host "Скачиваю модель: $Model (может занять 10-30 минут)…"
& ollama pull $Model
Write-Host "[OK] Модель '$Model' готова." -ForegroundColor Green

# ── 4. Python dependencies ────────────────────────────────────────────────────
if (-not $SkipPyDeps) {
    Write-Host "`n[4/5] Устанавливаю Python-библиотеки…" -ForegroundColor Yellow
    Push-Location $Root
    try {
        $venvPath = Join-Path $Root ".venv"
        if (-not (Test-Path $venvPath)) {
            & $pythonCmd.Source -m venv .venv
        }
        $pip = Join-Path $venvPath "Scripts\pip.exe"
        & $pip install --upgrade pip

        # Для рабочей машины (не GPU) ставим только лёгкий набор — без PyTorch.
        # PyTorch и обучение нужны только на GPU-машине (см. install_training_host.ps1).
        $reqInf = Join-Path $Root "requirements-inference.txt"
        if (Test-Path $reqInf) {
            Write-Host "Ставлю набор для инференса (без PyTorch)…"
            & $pip install -r $reqInf
        } else {
            & $pip install -r requirements.txt
        }
        Write-Host "[OK] Библиотеки установлены в .venv" -ForegroundColor Green
    } finally {
        Pop-Location
    }
} else {
    Write-Host "`n[4/5] Пропустил установку Python-библиотек (--SkipPyDeps)." -ForegroundColor DarkGray
}

# ── 5. Initialise DB ──────────────────────────────────────────────────────────
Write-Host "`n[5/5] Создаю базу данных SQLite…" -ForegroundColor Yellow
$pyExe = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $pyExe) {
    Push-Location $Root
    $env:PYTHONIOENCODING = "utf-8"
    try {
        & $pyExe main.py init-db
    } finally {
        Pop-Location
    }
} else {
    Write-Warning "venv не найден — БД не создана. Запустите вручную: python main.py init-db"
}

Write-Host "`n════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  УСТАНОВКА ЗАВЕРШЕНА" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "Дальше: двойной клик на файл ЗАПУСК.bat" -ForegroundColor White
Write-Host "В браузере откроется http://localhost:7860" -ForegroundColor White
Write-Host ""
