param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ManagerArgs
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir
$ManagerScript = Join-Path $ScriptDir 'hhan_pzone_manager.py'

if (-not (Test-Path -LiteralPath $ManagerScript)) {
    Write-Host ('[ERROR] Manager script not found: ' + $ManagerScript)
    exit 2
}

function Test-Python3 {
    param([string]$Exe, [string[]]$Prefix = @())
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
$Candidates = @(
    @{ Name = 'py.exe'; Prefix = @('-3') },
    @{ Name = 'python.exe'; Prefix = @() },
    @{ Name = 'python3.exe'; Prefix = @() }
)

foreach ($Item in $Candidates) {
    if ($SelectedExe) { break }
    $Cmd = Get-Command $Item.Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Cmd -and (Test-Python3 -Exe $Cmd.Source -Prefix $Item.Prefix)) {
        $SelectedExe = $Cmd.Source
        $SelectedPrefix = $Item.Prefix
    }
}

if (-not $SelectedExe) {
    Write-Host '[ERROR] No usable Python 3 installation was found.'
    exit 127
}

if (-not $ManagerArgs -or $ManagerArgs.Count -eq 0) {
    $ManagerArgs = @('--execute')
}

Write-Host ('[INFO] Python: ' + $SelectedExe)
Write-Host ('[INFO] Args: ' + ($ManagerArgs -join ' '))

& $SelectedExe @SelectedPrefix $ManagerScript @ManagerArgs
$Code = $LASTEXITCODE
if ($null -eq $Code) { $Code = 1 }
exit $Code
