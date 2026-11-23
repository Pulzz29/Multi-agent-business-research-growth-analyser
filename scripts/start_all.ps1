# Start InsightIQ (FastAPI backend + Vite frontend) in two new PowerShell windows.
# Usage:  .\scripts\start_all.ps1            (uses .env)
#         .\scripts\start_all.ps1 -EnvFile .env.test
param([string]$EnvFile = ".env")

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Virtual environment missing. See migration_reports\LOCAL_STARTUP_GUIDE.md" }
if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
    Push-Location (Join-Path $root "frontend"); npm ci --no-audit --no-fund; Pop-Location
}

Start-Process powershell -WorkingDirectory $root -ArgumentList @(
    "-NoExit", "-Command", "& '$python' scripts\run_backend.py --env-file '$EnvFile'")
Start-Process powershell -WorkingDirectory (Join-Path $root "frontend") -ArgumentList @(
    "-NoExit", "-Command", "npm run dev")

Write-Host "Backend:  http://127.0.0.1:8000/health/ready"
Write-Host "Frontend: http://127.0.0.1:5173  (enter API_ACCESS_KEY from $EnvFile)"
