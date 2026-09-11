$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
$ManagerScript = Join-Path $ScriptDir 'hhan_pzone_manager.py'

if (-not (Test-Path -LiteralPath $ManagerScript)) {
    Write-Host ('[ERROR] Manager script not found: ' + $ManagerScript)
    exit 2
}

function Test-Python3 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Exe,
        [string[]]$Prefix = @()
    )

    try {
        & $Exe @Prefix -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' > $null 2>&1
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

$SelectedExe = $null
$SelectedPrefix = @()

$CommandCandidates = @(
    @{ Name = 'py.exe'; Prefix = @('-3') },
    @{ Name = 'python.exe'; Prefix = @() },
    @{ Name = 'python3.exe'; Prefix = @() }
)

foreach ($Item in $CommandCandidates) {
    if ($SelectedExe) {
        break
    }

    $Cmd = Get-Command $Item.Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Cmd -and (Test-Python3 -Exe $Cmd.Source -Prefix $Item.Prefix)) {
        $SelectedExe = $Cmd.Source
        $SelectedPrefix = $Item.Prefix
    }
}

if (-not $SelectedExe) {
    $LocalPatterns = @(
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-*\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe')
    )

    foreach ($Pattern in $LocalPatterns) {
        if ($SelectedExe) {
            break
        }

        $Matches = Get-ChildItem -Path $Pattern -File -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($Match in $Matches) {
            if (Test-Python3 -Exe $Match.FullName) {
                $SelectedExe = $Match.FullName
                $SelectedPrefix = @()
                break
            }
        }
    }
}

if (-not $SelectedExe) {
    Write-Host '[ERROR] No usable Python 3 installation was found.'
    Write-Host 'Try these commands in PowerShell:'
    Write-Host '  py -3 --version'
    Write-Host '  python --version'
    exit 127
}

$PrefixText = $SelectedPrefix -join ' '
if ($PrefixText) {
    Write-Host ('[INFO] Python: ' + $SelectedExe + ' ' + $PrefixText)
}
else {
    Write-Host ('[INFO] Python: ' + $SelectedExe)
}

& $SelectedExe @SelectedPrefix $ManagerScript --execute
$Code = $LASTEXITCODE

if ($null -eq $Code) {
    $Code = 1
}

exit $Code
