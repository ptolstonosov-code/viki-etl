# ─────────────────────────────────────────────────────────────────
# Импортирует обученную модель (.gguf + Modelfile) в Ollama
# и переключает конфиг на неё.
# ─────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Incoming = Join-Path $Root "models\incoming"

if (-not (Test-Path $Incoming)) {
    New-Item -ItemType Directory -Force -Path $Incoming | Out-Null
    Write-Host "Создал папку $Incoming" -ForegroundColor Yellow
    Write-Host "Положите туда файлы .gguf и Modelfile и запустите скрипт заново." -ForegroundColor Yellow
    exit 1
}

# Ищем файлы
$ggufFiles = Get-ChildItem -Path $Incoming -Filter "*.gguf" -File
$modelfiles = Get-ChildItem -Path $Incoming -Filter "Modelfile*" -File

if ($ggufFiles.Count -eq 0) {
    Write-Error "В $Incoming не найдено ни одного файла .gguf. Положите туда обученную модель."
    exit 1
}
if ($modelfiles.Count -eq 0) {
    Write-Error "В $Incoming не найдено файла Modelfile. Положите туда настройки модели."
    exit 1
}

$gguf = $ggufFiles | Sort-Object Length -Descending | Select-Object -First 1
$modelfile = $modelfiles | Select-Object -First 1

Write-Host "[*] Найдено:"
Write-Host "    модель:      $($gguf.Name) ($([math]::Round($gguf.Length/1MB,1)) МБ)"
Write-Host "    настройки:   $($modelfile.Name)"
Write-Host ""

# Имя модели — берём из имени файла Modelfile
$modelName = $modelfile.Name -replace "^Modelfile\.?", ""
if (-not $modelName) { $modelName = "etl-parser" }

# Modelfile с GPU-машины ссылается на абсолютный путь .gguf, который у нас другой —
# перепишем строку FROM на актуальный локальный путь.
Write-Host "[*] Подгоняю Modelfile под локальные пути..."
$content = Get-Content $modelfile.FullName -Raw -Encoding UTF8
$content = $content -replace '^FROM\s+.+$', "FROM `"$($gguf.FullName)`""
$fixedModelfile = Join-Path $Incoming "Modelfile.local"
[System.IO.File]::WriteAllText($fixedModelfile, $content, [System.Text.UTF8Encoding]::new($false))

# Проверяем что Ollama запущен
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not (Test-Path $ollamaExe)) {
    Write-Error "Ollama не установлена. Запустите ПЕРВЫЙ_ЗАПУСК.bat"
    exit 1
}
if (-not (Get-Process "ollama" -ErrorAction SilentlyContinue)) {
    Write-Host "[*] Запускаю Ollama..."
    Start-Process $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

# Регистрируем модель
Write-Host "[*] Регистрирую модель '$modelName' в Ollama..."
& $ollamaExe create $modelName -f $fixedModelfile
if ($LASTEXITCODE -ne 0) {
    Write-Error "Ollama вернула ошибку при регистрации. Проверьте Modelfile."
    exit 1
}
Write-Host ""
Write-Host "[OK] Модель зарегистрирована." -ForegroundColor Green

# Обновляем config\model.yaml — меняем имя модели на новую
$modelYamlPath = Join-Path $Root "config\model.yaml"
$yamlText = Get-Content $modelYamlPath -Raw -Encoding UTF8
$newYaml = $yamlText -replace '(?m)^(\s*model:\s*)"[^"]+"', "`${1}`"$modelName`""
[System.IO.File]::WriteAllText($modelYamlPath, $newYaml, [System.Text.UTF8Encoding]::new($false))

Write-Host "[OK] В config\model.yaml выбрана модель: $modelName" -ForegroundColor Green
Write-Host ""
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  ГОТОВО! Перезапустите программу через ЗАПУСК.bat" -ForegroundColor Green
Write-Host "  Парсер теперь использует вашу обученную модель." -ForegroundColor Green
Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Green
