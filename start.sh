#!/usr/bin/env bash
# start.sh - Launch the Claude Code Web (ACP) application
set -euo pipefail

projectDir="$(cd "$(dirname "$0")" && pwd)"

# Read config.toml for defaults
HostAddr=""
Port=""
configPath="$projectDir/config.toml"
if [ -f "$configPath" ]; then
    if [ -z "$HostAddr" ]; then
        HostAddr=$(grep -oP 'host\s*=\s*"\K[^"]+' "$configPath" || true)
    fi
    if [ -z "$Port" ]; then
        Port=$(grep -oP 'port\s*=\s*\K\d+' "$configPath" || true)
    fi
fi
: "${HostAddr:=127.0.0.1}"
: "${Port:=8001}"

echo "========================================"
echo "  Claude Code Web (ACP) - Starting Server"
echo "========================================"

# Create virtual environment if needed
venvPath="$projectDir/.venv"
if [ ! -d "$venvPath" ]; then
    echo "[*] Creating Python virtual environment..."
    python3 -m venv "$venvPath"
fi

# Activate virtual environment
source "$venvPath/bin/activate"

# Install/update Python requirements
reqFile="$projectDir/requirements.txt"
echo "[*] Installing Python dependencies..."
pip install -r "$reqFile" --quiet

# Install/update npm requirements (for @agentclientprotocol/claude-agent-acp)
packageJson="$projectDir/package.json"
if [ -f "$packageJson" ]; then
    echo "[*] Installing npm dependencies..."
    (cd "$projectDir" && npm install --no-audit --no-fund)
fi

# Start the server
echo "[*] Starting FastAPI server at http://${HostAddr}:${Port}"
echo "[*] Press Ctrl+C to stop"
echo ""

export CLAUDE_CODE_WEB_PORT="$Port"
cd "$projectDir"
uvicorn main:app --host "$HostAddr" --port "$Port" --log-level info
