$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $projectRoot 'backend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'
$pidFile = Join-Path $projectRoot '.runtime\processes.json'
if (-not (Test-Path $venvPython)) { throw 'Chua co moi truong Python. Hay chay start-app.bat mot lan truoc.' }

# Refuse while anything started by start-app.bat is still alive (dbtools also checks the database itself).
if (Test-Path $pidFile) {
    foreach ($entry in @(Get-Content $pidFile -Raw | ConvertFrom-Json)) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if ($process -and $process.StartTime.ToUniversalTime().ToString('o') -eq $entry.startedAt) {
            Write-Host 'Ung dung dang chay. Hay chay stop-app.bat truoc khi khoi phuc database.'; exit 1
        }
    }
}

Push-Location $backend
try {
    $backups = @(& $venvPython -m app.dbtools list)
    if ($backups.Count -eq 0) { Write-Host 'Khong co ban backup nao trong .runtime\backups.'; exit 1 }
    Write-Host 'Cac ban backup (moi nhat truoc):'
    for ($i = 0; $i -lt $backups.Count; $i++) { Write-Host ("  [{0}] {1}" -f ($i + 1), $backups[$i]) }
    $choice = Read-Host 'Nhap so ban muon khoi phuc (Enter de huy)'
    if (-not $choice) { Write-Host 'Da huy.'; exit 0 }
    $index = 0
    if (-not [int]::TryParse($choice, [ref]$index) -or $index -lt 1 -or $index -gt $backups.Count) { Write-Host 'Lua chon khong hop le.'; exit 1 }
    $name = $backups[$index - 1]
    $confirm = Read-Host "Database hien tai se duoc luu thanh ban an toan roi thay bang $name. Go YES de tiep tuc"
    if ($confirm -ne 'YES') { Write-Host 'Da huy.'; exit 0 }
    & $venvPython -m app.dbtools restore $name
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host 'Xong. Chay start-app.bat de khoi dong lai (migration se tu chay neu can).'
} finally { Pop-Location }
