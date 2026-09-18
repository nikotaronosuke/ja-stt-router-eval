param([switch]$ApprovedPackages)
$ErrorActionPreference = 'Stop'
if (-not $ApprovedPackages) { throw 'Explicit package installation approval required.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:UV_CACHE_DIR = Join-Path $projectRoot '.cache\uv'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv --without-pip .venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
uv --system-certs pip install --python '.venv\Scripts\python.exe' -r requirements-windows.txt
if ($LASTEXITCODE -ne 0) { throw 'dependency installation failed' }
& '.venv\Scripts\python.exe' -c 'import numpy, scipy, websockets, pyaudiowpatch, psutil, tkinter; print("windows_dependencies_import_ok")'
if ($LASTEXITCODE -ne 0) { throw 'dependency verification failed' }
