# One-time setup of an InsightIQ deployment package on a new Windows PC.
# Run from the extracted folder:   powershell -ExecutionPolicy Bypass -File .\scripts\setup_remote.ps1
#
# 1. Creates .venv with Python 3.13 and installs the pinned dependencies.
# 2. Installs frontend packages from the lockfile (npm ci).
# 3. Creates .env from .env.example (if missing) with a new random API_ACCESS_KEY and the
#    INSIGHTIQ_LEGACY_PROJECT_ROOTS entry needed for paths recorded on the source PC.
# 4. If AZURE/NEWSDATA/SEC values are filled in, prepares the local runtime database.
# Makes no Azure OpenAI or NewsData.io calls.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$manifest = Get-Content (Join-Path $root "deployment_manifest.json") -Raw | ConvertFrom-Json

# 1. Python environment
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv (Python 3.13)..."
    py -3.13 -m venv .venv
    if (-not $?) { throw "Python 3.13 not found. Install it from python.org, then rerun." }
}
& $venvPython -m pip install --disable-pip-version-check -q -r requirements.lock.txt
if (-not $?) { throw "pip install failed" }
& $venvPython -m pip check

# 2. Frontend packages
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "Node.js/npm not found. Install Node.js 20+ and rerun." }
Push-Location (Join-Path $root "frontend"); npm ci --no-audit --no-fund; Pop-Location

# 3. .env
$envPath = Join-Path $root ".env"
if (-not (Test-Path $envPath)) {
    $key = & $venvPython -c "import secrets; print(secrets.token_urlsafe(48))"
    $lines = Get-Content (Join-Path $root ".env.example") | ForEach-Object {
        if ($_ -match '^API_ACCESS_KEY=') { "API_ACCESS_KEY=$key" } else { $_ }
    }
    $lines += ""
    $lines += "# Paths stored on the source PC are rebased onto this folder (see BUG_REPORT B11)."
    $lines += "INSIGHTIQ_LEGACY_PROJECT_ROOTS=$($manifest.source_project_root)"
    [System.IO.File]::WriteAllLines($envPath, $lines)
    Write-Host "Created .env with a new API_ACCESS_KEY."
}

# 4. Runtime database (needs real keys because settings validation requires them)
$values = & $venvPython -c "from dotenv import dotenv_values as d; v=d('.env'); print(all((v.get(k) or '').strip() for k in ('AZURE_OPENAI_API_KEY','AZURE_OPENAI_ENDPOINT','AZURE_OPENAI_DEPLOYMENT','NEWSDATA_API_KEY','SEC_USER_AGENT')))"
if ($values -eq "True") {
    & $venvPython scripts\prepare_local_runtime.py
    Write-Host ""
    Write-Host "Setup complete. Start InsightIQ with:  .\scripts\start_all.ps1"
} else {
    Write-Host ""
    Write-Host "Next: open .env, fill AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, NEWSDATA_API_KEY and SEC_USER_AGENT, save, then run:"
    Write-Host "  .\.venv\Scripts\python.exe scripts\prepare_local_runtime.py"
    Write-Host "  .\scripts\start_all.ps1"
}
