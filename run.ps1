$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
$ManagerScript = Join-Path $ScriptDir 'hhan_pzone_manager.py'

if (-not (Test-Path -LiteralPath $ManagerScript)) {
    Write-Host ('[ERROR] Manager script not found: ' + $ManagerScript)
    exit 2
}

function Try-Python3 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Exe,
        [string[]]$Prefix = @()
    )

    try {
        & $Exe @Prefix -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' > $null 2>&1
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
    }
    catch {
        return $false
    }

    $PrefixText = $Prefix -join ' '
    if ($PrefixText) {
        Write-Host ('[INFO] Python: ' + $Exe + ' ' + $PrefixText)
    }
    else {
        Write-Host ('[INFO] Python: ' + $Exe)
    }

    & $Exe @Prefix $ManagerScript --execute
    $Code = $LASTEXITCODE
    if ($null -eq $Code) {
        $Code = 1
    }
    exit $Code
}

$CommandCandidates = @(
    @{ Name = 'py.exe'; Prefix = @('-3') },
    @{ Name = 'python.exe'; Prefix = @() },
    @{ Name = 'python3.exe'; Prefix = @() }
)

foreach ($Item in $CommandCandidates) {
    $Cmd = Get-Command $Item.Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Cmd) {
        Try-Python3 -Exe $Cmd.Source -Prefix $Item.Prefix | Out-Null
    }
}

$LocalPatterns = @(
    (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-*\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe')
)

foreach ($Pattern in $LocalPatterns) {
    $Matches = Get-ChildItem -Path $Pattern -File -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
    foreach ($Match in $Matches) {
        Try-Python3 -Exe $Match.FullName | Out-Null
    }
}

Write-Host '[ERROR] No usable Python 3 installation was found.'
Write-Host 'Try these commands in PowerShell:'
Write-Host '  py -3 --version'
Write-Host '  python --version'
exit 127
