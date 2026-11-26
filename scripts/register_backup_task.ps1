# Registers a daily Windows scheduled task that runs scripts\backup_postgres.py
# (backup + restore verification + retention). Review, then run once from an
# elevated PowerShell in the project folder:
#   powershell -ExecutionPolicy Bypass -File .\scripts\register_backup_task.ps1 [-At "02:30"] [-ExportSqlite]
param([string]$At = "02:30", [string]$TaskName = "InsightIQ PostgreSQL backup", [switch]$ExportSqlite)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "scripts\backup_postgres.py"
$log = Join-Path $root "logs\backup_postgres.log"
$extra = if ($ExportSqlite) { " --export-sqlite" } else { "" }
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$python`" `"$script`"$extra >> `"$log`" 2>&1`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Registered '$TaskName' daily at $At. Log: $log"
