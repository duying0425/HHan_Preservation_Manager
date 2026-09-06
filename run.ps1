# ==============================================================================
# HHanClub 保种区自动化综合管理运行脚本 (run.ps1)
# ==============================================================================
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$utf8 = New-Object System.Text.UTF8Encoding($false)

$LogDir = Join-Path $ScriptDir "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}
$LogFile = Join-Path $LogDir "manager.log"

$PythonExe = "C:\Users\duyin\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonCmd = Get-Command py, python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($PythonCmd) {
        $PythonExe = $PythonCmd.Source
    } else {
        $PythonExe = "python"
    }
}

$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$Header = @"
================================================================================
[$Timestamp] 开始执行 HHanClub 保种区自动化综合管理流水线
================================================================================
"@
Write-Host $Header -ForegroundColor Cyan
[System.IO.File]::AppendAllText($LogFile, $Header + "`r`n", $utf8)

$ManagerScript = Join-Path $ScriptDir "hhan_pzone_manager.py"

$processInfo = New-Object System.Diagnostics.ProcessStartInfo
$processInfo.FileName = $PythonExe
$processInfo.Arguments = "`"$ManagerScript`" --execute"
$processInfo.WorkingDirectory = $ScriptDir
$processInfo.RedirectStandardOutput = $true
$processInfo.RedirectStandardError = $true
$processInfo.UseShellExecute = $false
$processInfo.CreateNoWindow = $true
$processInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$processInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $processInfo
$process.Start() | Out-Null

$buffer = New-Object System.Text.StringBuilder
while (-not $process.HasExited) {
    $line = $process.StandardOutput.ReadLine()
    if ($null -ne $line) {
        [Console]::WriteLine($line)
        [System.IO.File]::AppendAllText($LogFile, $line + "`r`n", $utf8)
    }
}
while (-not $process.StandardOutput.EndOfStream) {
    $line = $process.StandardOutput.ReadLine()
    [Console]::WriteLine($line)
    [System.IO.File]::AppendAllText($LogFile, $line + "`r`n", $utf8)
}
while (-not $process.StandardError.EndOfStream) {
    $errLine = $process.StandardError.ReadLine()
    [Console]::WriteLine($errLine)
    [System.IO.File]::AppendAllText($LogFile, "[ERROR] " + $errLine + "`r`n", $utf8)
}

$ExitCode = $process.ExitCode
$EndTimestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$Footer = @"
================================================================================
[$EndTimestamp] 执行完成，退出状态码: $ExitCode
================================================================================
"@
Write-Host $Footer -ForegroundColor Cyan
[System.IO.File]::AppendAllText($LogFile, $Footer + "`r`n`r`n", $utf8)
exit $ExitCode