# Packages the project into a ZIP for transfer to the GPU training host.
# Excludes venv, DB, downloaded models, __pycache__, etc.
# This script intentionally uses only ASCII identifiers internally
# (file names with Cyrillic are matched via wildcards from the filesystem).

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$Root = Split-Path -Parent $PSScriptRoot
$OutZip = Join-Path $Root "llm_etl_for_training.zip"
$Staging = Join-Path $env:TEMP "llm_etl_pkg_$(Get-Random)"

Write-Host "[*] Staging: $Staging"
New-Item -ItemType Directory -Force -Path $Staging | Out-Null

function Copy-Tree($src, $dst) {
    if (-not (Test-Path $src)) { return }
    $parent = Split-Path -Parent $dst
    if ($parent -and -not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    # robocopy is more reliable than Copy-Item for excluding folders
    if ((Get-Item $src).PSIsContainer) {
        robocopy $src $dst /E /XD __pycache__ .venv .git models data > $null
    } else {
        Copy-Item $src $dst -Force
    }
}

# Core code & data
Copy-Tree (Join-Path $Root "config")                  (Join-Path $Staging "config")
Copy-Tree (Join-Path $Root "core")                    (Join-Path $Staging "core")
Copy-Tree (Join-Path $Root "training")                (Join-Path $Staging "training")
Copy-Tree (Join-Path $Root "scripts")                 (Join-Path $Staging "scripts")
Copy-Tree (Join-Path $Root "ui")                      (Join-Path $Staging "ui")
Copy-Tree (Join-Path $Root "gpu_host")                (Join-Path $Staging "gpu_host")
Copy-Tree (Join-Path $Root "target_host")             (Join-Path $Staging "target_host")
Copy-Tree (Join-Path $Root "examples\training_data")  (Join-Path $Staging "examples\training_data")

# Sample files
$examplesDst = Join-Path $Staging "examples"
if (-not (Test-Path $examplesDst)) { New-Item -ItemType Directory -Force -Path $examplesDst | Out-Null }
Get-ChildItem -Path (Join-Path $Root "examples") -Filter "sample_*" -File -ErrorAction SilentlyContinue |
    ForEach-Object { Copy-Item $_.FullName $examplesDst -Force }

# NOTE: GPU host is Ubuntu — Linux-side install is gpu_host/1_install.sh.
# We deliberately do NOT ship setup/install*.ps1 (Windows-only installers).

# Top-level files for the GPU host (Ubuntu): only Python entry-point + reqs + README.
# .bat files stay on the workstation — they're useless on Linux.
foreach ($pattern in @("main.py", "requirements.txt", "*.txt")) {
    Get-ChildItem -Path $Root -Filter $pattern -File -ErrorAction SilentlyContinue | ForEach-Object {
        $dst = Join-Path $Staging $_.Name
        Copy-Item $_.FullName $dst -Force
        Write-Host "  + $($_.Name)"
    }
}

# Build the archive
if (Test-Path $OutZip) { Remove-Item $OutZip -Force }
Write-Host "[*] Creating archive..."
Compress-Archive -Path "$Staging\*" -DestinationPath $OutZip -CompressionLevel Optimal -Force

Remove-Item $Staging -Recurse -Force

$sizeKB = [math]::Round((Get-Item $OutZip).Length / 1KB, 1)
Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  [OK] Archive ready: $OutZip ($sizeKB KB)" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host "What to do next:"
Write-Host "  1. Copy this ZIP file to the GPU machine (RTX 5090)."
Write-Host "  2. Unzip it anywhere."
Write-Host "  3. Open PROCHTI_MENYA.txt (or the Cyrillic-named text file) inside."
