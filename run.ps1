# Launch Macgear Reporting (was Vendor Credit Claims / apps.macgeargroup.com) on http://127.0.0.1:8030
# First run: it auto-creates the DB and seeds reference data if needed.
#
# Local dev port is 8030 (was 8000, which collided with 3PL Portal and stopped
# the two running side by side). The container still serves on 8000 internally —
# do not change the Docker/Caddy/n8n references.
#
# Reports read the warehouse through REPORTING_DB_URL in a gitignored .env
# (template: .env.example). Without it the app runs, but reports say the
# warehouse could not be reached.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (Test-Path ".env") {
    foreach ($line in Get-Content ".env") {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2].Trim()
        }
    }
}

if (-not (Test-Path "data\app.db")) {
    Write-Host "Seeding database from template..."
    .\.venv\Scripts\python.exe scripts\extract_seed.py
    .\.venv\Scripts\python.exe -m app.seed
}

Write-Host "Starting app at http://127.0.0.1:8030  (Ctrl+C to stop)"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8030
