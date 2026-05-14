# start.ps1 - Launch the Claude Code Web (ACP) application
param(
    [int]$Port = 0,
    [string]$HostAddr = ""
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Read config.toml for defaults
$configPath = Join-Path $projectDir "config.toml"
if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw
    if ($HostAddr -eq "") {
        $match = [regex]::Match($config, 'host\s*=\s*"([^"]+)"')
        if ($match.Success) { $HostAddr = $match.Groups[1].Value }
    }
    if ($Port -eq 0) {
        $match = [regex]::Match($config, 'port\s*=\s*(\d+)')
        if ($match.Success) { $Port = [int]$match.Groups[1].Value }
    }
}
if ($HostAddr -eq "") { $HostAddr = "127.0.0.1" }
if ($Port -eq 0) { $Port = 8001 }

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Claude Code Web (ACP) - Starting Server" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Create virtual environment if needed
$venvPath = Join-Path $projectDir ".venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "[*] Creating Python virtual environment..." -ForegroundColor Yellow
    python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: Failed to create virtual environment. Ensure Python 3.11+ is installed." -ForegroundColor Red
        exit 1
    }
}

# Activate virtual environment
$activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
if (-not (Test-Path $activateScript)) {
    Write-Host "Error: Virtual environment activation script not found at $activateScript" -ForegroundColor Red
    exit 1
}
. $activateScript

# Install/update Python requirements
$reqFile = Join-Path $projectDir "requirements.txt"
Write-Host "[*] Installing Python dependencies..." -ForegroundColor Yellow
pip install -r $reqFile --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: Failed to install Python requirements." -ForegroundColor Red
    exit 1
}

# Install/update npm requirements (for @agenticprotocol/claude-agent-acp)
$packageJson = Join-Path $projectDir "package.json"
if (Test-Path $packageJson) {
    Write-Host "[*] Installing npm dependencies..." -ForegroundColor Yellow
    Push-Location $projectDir
    try {
        npm install --no-audit --no-fund 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Warning: npm install had issues, but continuing..." -ForegroundColor Yellow
        } else {
            Write-Host "[*] npm dependencies installed." -ForegroundColor Green
        }
    } finally {
        Pop-Location
    }
}

# Start the server
Write-Host "[*] Starting FastAPI server at http://${HostAddr}:${Port}" -ForegroundColor Green
Write-Host "[*] Press Ctrl+C to stop" -ForegroundColor Green
Write-Host ""

Set-Location $projectDir
$env:CLAUDE_CODE_WEB_PORT = $Port
uvicorn main:app --host $HostAddr --port $Port --log-level info
