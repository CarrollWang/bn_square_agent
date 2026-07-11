$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot "ms-playwright"

try {
    & .\.venv\Scripts\python.exe -B .\export-binance-cookie.py
    if ($LASTEXITCODE -ne 0) {
        throw "Cookie 导出工具退出码：$LASTEXITCODE"
    }
}
catch {
    Write-Host "Cookie export failed: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}
