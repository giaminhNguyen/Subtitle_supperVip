$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot '.runtime\processes.json'
if (-not (Test-Path $pidFile)) { Write-Host 'Khong co tien trinh nao do start-app.bat tao.'; exit 0 }
$entries = Get-Content $pidFile -Raw | ConvertFrom-Json
foreach ($entry in @($entries)) {
    $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().ToString('o') -eq $entry.startedAt) {
        Stop-Process -Id $entry.pid -ErrorAction SilentlyContinue
        Write-Host "Da dung $($entry.name)."
    }
}
Remove-Item -LiteralPath $pidFile -Force
