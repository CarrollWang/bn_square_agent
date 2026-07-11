$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot "ms-playwright"

$existing = Get-NetTCPConnection -LocalPort 8788 -ErrorAction SilentlyContinue |
  Where-Object { $_.State -eq "Listen" } |
  Select-Object -First 1

if ($existing) {
  Write-Host "Port 8788 is already in use by PID $($existing.OwningProcess)." -ForegroundColor Yellow
  exit 1
}

Write-Host "Starting BN Square Agent MCP at http://127.0.0.1:8788/mcp"
Write-Host "Close this PowerShell window or press Ctrl+C to stop."

.\.venv\Scripts\python.exe -B run.py serve-mcp --host 127.0.0.1 --port 8788