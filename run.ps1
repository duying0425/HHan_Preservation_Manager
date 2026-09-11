# HHanClub 保种区自动化运行脚本
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) {
    & $Python.Source -3 "$ScriptDir/hhan_pzone_manager.py" --execute
    exit $LASTEXITCODE
}

$Python = Get-Command python3, python -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Python) {
    & $Python.Source "$ScriptDir/hhan_pzone_manager.py" --execute
    exit $LASTEXITCODE
}

Write-Error "Python 3 not found"
exit 127
