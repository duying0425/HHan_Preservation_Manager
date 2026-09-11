# HHanClub 保种区自动化运行脚本
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$ErrorActionPreference = "Stop"
$ManagerScript = Join-Path $ScriptDir "hhan_pzone_manager.py"

if (-not (Test-Path $ManagerScript)) {
    Write-Error "Manager script not found: $ManagerScript"
    exit 2
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [string[]]$PrefixArgs = @()
    )

    try {
        & $Executable @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

$Candidates = @()

# 1) Python Launcher. py.exe 可能存在但没有有效 Python 注册，因此必须实际探测。
$PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($PyLauncher) {
    $Candidates += [PSCustomObject]@{
        Exe = $PyLauncher.Source
        Prefix = @("-3")
        Label = "py -3"
    }
}

# 2) PATH 中的 python/python3。WindowsApps 的占位别名会在实际探测时自动被排除。
foreach ($CommandName in @("python.exe", "python3.exe", "python")) {
    $Cmd = Get-Command $CommandName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Cmd) {
        $Candidates += [PSCustomObject]@{
            Exe = $Cmd.Source
            Prefix = @()
            Label = $CommandName
        }
    }
}

# 3) 常见用户级 Python 安装目录兜底，不写死用户名和 Python 版本。
$SearchPatterns = @(
    (Join-Path $env:LOCALAPPDATA "Python\pythoncore-*\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe")
)
foreach ($Pattern in $SearchPatterns) {
    Get-ChildItem -Path $Pattern -File -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object {
        $Candidates += [PSCustomObject]@{
            Exe = $_.FullName
            Prefix = @()
            Label = $_.FullName
        }
    }
}

# 去重后逐个进行真实 Python 3 探测。
$Seen = @{}
$Selected = $null
foreach ($Candidate in $Candidates) {
    $Key = $Candidate.Exe + "|" + ($Candidate.Prefix -join " ")
    if ($Seen.ContainsKey($Key)) {
        continue
    }
    $Seen[$Key] = $true

    if (Test-PythonCandidate -Executable $Candidate.Exe -PrefixArgs $Candidate.Prefix) {
        $Selected = $Candidate
        break
    }
}

if (-not $Selected) {
    Write-Host "[ERROR] 未找到可用的 Python 3。" -ForegroundColor Red
    Write-Host "请在 PowerShell 中执行以下命令检查：" -ForegroundColor Yellow
    Write-Host "  py -3 --version"
    Write-Host "  python --version"
    exit 127
}

Write-Host ("[INFO] 使用 Python: {0} {1}" -f $Selected.Exe, ($Selected.Prefix -join " ")) -ForegroundColor Cyan

try {
    & $Selected.Exe @($Selected.Prefix) $ManagerScript --execute
    $ExitCode = $LASTEXITCODE
}
catch {
    Write-Error $_
    exit 1
}

if ($null -eq $ExitCode) {
    $ExitCode = 1
}

exit $ExitCode
