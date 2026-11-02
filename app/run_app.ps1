# Start the TFS web app with the pipeline's virtual environment.
#   powershell -ExecutionPolicy Bypass -File .\run_app.ps1
# Then open http://127.0.0.1:5000
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $here "..\Work with claude\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Pipeline virtual environment not found: $python" -ForegroundColor Red
    Write-Host "Run setup_env.ps1 in 'Work with claude' first, or set TFS_PROJECT_DIR." -ForegroundColor Yellow
    exit 1
}
Set-Location $here
& $python app.py
