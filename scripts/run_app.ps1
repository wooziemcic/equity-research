$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptRoot "..")
$VenvActivate = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"

Push-Location $ProjectRoot
try {
    if (Test-Path $VenvActivate) {
        . $VenvActivate
        Write-Host "Activated virtual environment: $VenvActivate"
    }
    else {
        Write-Host "No .venv found. Create one with: python -m venv .venv"
        Write-Host "Continuing with the current PowerShell Python environment."
    }

    python -m streamlit run app\Home.py --server.port 8505
}
finally {
    Pop-Location
}
