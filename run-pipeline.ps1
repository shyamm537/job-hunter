#requires -Version 5
<#
    run-pipeline.ps1 — run the whole Job Hunter AI pipeline, in order, on Windows.

    Equivalent to:
        make discover  ->  python -m src.ingestion.discover
        make validate  ->  python -m src.ingestion.validate
        make scrape    ->  python -m src.ingestion.cli
        make contacts  ->  python -m src.contacts.cli
        make process   ->  python -m src.llm.cli
        make app       ->  python -m streamlit run src/app/main.py --server.address localhost

    Usage (from the repo root):
        .\run-pipeline.ps1
        .\run-pipeline.ps1 -Python C:\path\to\python.exe   # force an interpreter
        .\run-pipeline.ps1 -SkipApp                         # stop before launching the dashboard

    If PowerShell blocks the script ("running scripts is disabled"), run it once as:
        powershell -ExecutionPolicy Bypass -File .\run-pipeline.ps1
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipApp
)

$ErrorActionPreference = "Stop"
# Always run from the repo root (where this script lives), so `python -m src...`
# resolves regardless of where you invoke it from.
Set-Location $PSScriptRoot

# --- Pick a Python interpreter -------------------------------------------------
# Priority: -Python arg > active venv > a venv dir in the repo > python on PATH.
if (-not $Python) {
    if ($env:VIRTUAL_ENV) {
        $Python = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    } elseif (Test-Path "job-hunter\Scripts\python.exe") {
        $Python = "job-hunter\Scripts\python.exe"
    } elseif (Test-Path "venv\Scripts\python.exe") {
        $Python = "venv\Scripts\python.exe"
    } else {
        $Python = "python"
    }
}
Write-Host "Job Hunter AI pipeline" -ForegroundColor Cyan
Write-Host "Using Python: $Python`n" -ForegroundColor Cyan

# --- Step runner ---------------------------------------------------------------
function Invoke-Step {
    param(
        [string]$Name,
        [string[]]$CmdArgs,
        [switch]$Optional   # optional steps warn and continue; others stop the run
    )
    Write-Host "==== $Name ====" -ForegroundColor Green
    & $Python @CmdArgs
    $code = $LASTEXITCODE
    Write-Host ""
    if ($code -ne 0) {
        if ($Optional) {
            Write-Host "WARNING: '$Name' exited $code — optional step, continuing.`n" -ForegroundColor Yellow
        } else {
            Write-Host "ERROR: '$Name' exited $code — stopping the pipeline." -ForegroundColor Red
            exit $code
        }
    }
}

# discover/validate/contacts are prep & enrichment — a hiccup there shouldn't
# block the core run, so they warn-and-continue. scrape and process are the
# core pipeline and stop on failure.
Invoke-Step "discover  (propose new boards)"   @("-m", "src.ingestion.discover") -Optional
Invoke-Step "validate  (check boards are live)" @("-m", "src.ingestion.validate") -Optional
Invoke-Step "scrape    (fetch postings)"        @("-m", "src.ingestion.cli")
Invoke-Step "contacts  (find a contact)"        @("-m", "src.contacts.cli") -Optional
Invoke-Step "process   (generate materials)"    @("-m", "src.llm.cli")

if ($SkipApp) {
    Write-Host "Done (skipped app). Launch the dashboard with:" -ForegroundColor Cyan
    Write-Host "    $Python -m streamlit run src/app/main.py --server.address localhost"
    exit 0
}

# app is last because it blocks: it starts the Streamlit server and runs until
# you press Ctrl+C. Bound to localhost (matches `make app`).
Invoke-Step "app       (launch dashboard)" @("-m", "streamlit", "run", "src/app/main.py", "--server.address", "localhost")
