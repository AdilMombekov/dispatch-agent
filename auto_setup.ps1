# Dispatch Agent — autonomous setup script
$ErrorActionPreference = 'Continue'
$ProjectDir = 'E:\dispatch'
$LogFile    = Join-Path $ProjectDir 'auto_setup.log'
$StatusFile = Join-Path $ProjectDir 'auto_setup.status'

function Log($msg) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line  = "[$stamp] $msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

Set-Content -Path $LogFile   -Value "=== auto_setup.ps1 started ===" -Encoding UTF8
Set-Content -Path $StatusFile -Value "RUNNING" -Encoding UTF8

# 1. taskkill ----------------------------------------------------------------
Log "STEP 1: taskkill /F /IM DispatchAgent.exe"
try {
    Get-Process DispatchAgent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath taskkill -ArgumentList '/F','/IM','DispatchAgent.exe' -Wait -NoNewWindow -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Log "taskkill done"
} catch {
    Log "taskkill exception (ignored): $($_.Exception.Message)"
}

# 2. Find Obsidian vault -----------------------------------------------------
Log "STEP 2: search for .obsidian folders"

$searchRoots = @(
    [Environment]::GetFolderPath('MyDocuments'),
    "$env:USERPROFILE\Documents",
    "$env:USERPROFILE\OneDrive\Documents",
    "$env:USERPROFILE\OneDrive\Документы",
    "$env:USERPROFILE\Desktop",
    "$env:USERPROFILE\Obsidian",
    "$env:USERPROFILE",
    "E:\",
    "D:\",
    "C:\Obsidian"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

$found = @()
foreach ($root in $searchRoots) {
    Log "scanning: $root"
    try {
        $hits = Get-ChildItem -Path $root -Directory -Filter '.obsidian' -Recurse -Force -ErrorAction SilentlyContinue -Depth 4
        foreach ($m in $hits) {
            $vault = $m.Parent.FullName
            if ($vault -and ($found -notcontains $vault)) {
                $found += $vault
                Log "  found vault: $vault"
            }
        }
    } catch {
        Log "  scan error: $($_.Exception.Message)"
    }
}

if ($found.Count -eq 0) {
    Log "NO .obsidian found, keeping existing config value."
    $vaultPath = $null
} else {
    $preferred = $found | Where-Object { $_ -match 'Obsidian' } | Select-Object -First 1
    if (-not $preferred) { $preferred = $found[0] }
    $vaultPath = $preferred -replace '\\','/'
    Log "selected vault: $vaultPath"
}

# 3. Update config.json ------------------------------------------------------
Log "STEP 3: update config.json"
$configPath = Join-Path $ProjectDir 'config.json'
try {
    $cfg = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $cfg.obsidian) {
        $cfg | Add-Member -NotePropertyName 'obsidian' -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    if ($vaultPath) {
        $cfg.obsidian | Add-Member -NotePropertyName 'vault_path' -NotePropertyValue $vaultPath -Force
    }
    $json = $cfg | ConvertTo-Json -Depth 10
    Set-Content -Path $configPath -Value $json -Encoding UTF8
    Log "config.json updated. obsidian.vault_path = $($cfg.obsidian.vault_path)"
} catch {
    Log "config.json update FAILED: $($_.Exception.Message)"
}

# 4. PyInstaller build -------------------------------------------------------
Log "STEP 4: pyinstaller build"
Push-Location $ProjectDir
try {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $pythonExe) { $pythonExe = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $pythonExe) { throw "python.exe not found in PATH" }
    Log "python: $pythonExe"

    & $pythonExe -m pip show pyinstaller *> $null
    if ($LASTEXITCODE -ne 0) {
        Log "installing PyInstaller..."
        & $pythonExe -m pip install --quiet pyinstaller 2>&1 | ForEach-Object { Log "  pip: $_" }
    }

    if (Test-Path "$ProjectDir\build") { Remove-Item "$ProjectDir\build" -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path "$ProjectDir\dist\DispatchAgent.exe") { Remove-Item "$ProjectDir\dist\DispatchAgent.exe" -Force -ErrorAction SilentlyContinue }

    Log "running: $pythonExe -m PyInstaller --noconfirm DispatchAgent.spec"
    $build = & $pythonExe -m PyInstaller --noconfirm DispatchAgent.spec 2>&1
    foreach ($l in $build) { Log "  pyi: $l" }

    if (Test-Path "$ProjectDir\dist\DispatchAgent.exe") {
        Log "build OK -> dist\DispatchAgent.exe"
    } else {
        Log "BUILD FAILED"
        Set-Content -Path $StatusFile -Value "BUILD_FAILED" -Encoding UTF8
        Pop-Location
        exit 2
    }
} catch {
    Log "build exception: $($_.Exception.Message)"
    Set-Content -Path $StatusFile -Value "BUILD_FAILED" -Encoding UTF8
    Pop-Location
    exit 3
}
Pop-Location

# 5. Launch DispatchAgent.exe in background ---------------------------------
Log "STEP 5: launch DispatchAgent.exe"
try {
    $exe = Join-Path $ProjectDir 'dist\DispatchAgent.exe'
    Start-Process -FilePath $exe -WorkingDirectory $ProjectDir -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $proc = Get-Process DispatchAgent -ErrorAction SilentlyContinue
    if ($proc) {
        Log "DispatchAgent.exe running (PID=$($proc.Id))"
        Set-Content -Path $StatusFile -Value "OK PID=$($proc.Id) VAULT=$vaultPath" -Encoding UTF8
    } else {
        Log "started but no PID detected"
        Set-Content -Path $StatusFile -Value "STARTED_NO_PID VAULT=$vaultPath" -Encoding UTF8
    }
} catch {
    Log "launch exception: $($_.Exception.Message)"
    Set-Content -Path $StatusFile -Value "LAUNCH_FAILED" -Encoding UTF8
    exit 4
}

Log "=== auto_setup.ps1 finished ==="
exit 0
