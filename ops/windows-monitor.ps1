#requires -Version 5.1
#requires -PSEdition Desktop
[CmdletBinding(DefaultParameterSetName = 'Report')]
param(
    [Parameter(ParameterSetName = 'Report')]
    [string]$ConfigPath = (Join-Path $env:ProgramData 'KervaxMonitor\config.json'),

    [Parameter(Mandatory, ParameterSetName = 'Preview')]
    [switch]$Preview,

    [Parameter(Mandatory, ParameterSetName = 'Install')]
    [switch]$Install,

    [Parameter(Mandatory, ParameterSetName = 'Install')]
    [string]$PanelUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ReportUri([string]$Url) {
    $uri = $null
    if (-not [uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne 'https' -or -not $uri.Host -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw 'Use an HTTPS panel URL without credentials, a query or a fragment.'
    }
    return [uri]($uri.AbsoluteUri.TrimEnd('/') + '/api/agent/report')
}

function New-Report {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -OperationTimeoutSec 10
    $computer = Get-CimInstance -ClassName Win32_ComputerSystem -OperationTimeoutSec 10
    $cpu = Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'" -OperationTimeoutSec 10
    if ($null -eq $cpu -or $null -eq $cpu.PercentProcessorTime -or
        $os.TotalVisibleMemorySize -le 0 -or $null -eq $os.FreePhysicalMemory -or $null -eq $os.LastBootUpTime) {
        throw 'Windows did not return CPU, memory or boot-time data.'
    }

    $memTotal = [long]$os.TotalVisibleMemorySize * 1KB
    $memFree = [Math]::Min($memTotal, [long]$os.FreePhysicalMemory * 1KB)
    $mountsByVolume = Get-CimInstance -ClassName Win32_MountPoint -OperationTimeoutSec 10 |
        Group-Object -Property { $_.Volume.DeviceID } -AsHashTable -AsString
    $disks = @(Get-CimInstance -ClassName Win32_Volume -Filter 'DriveType=3' -OperationTimeoutSec 10 | ForEach-Object {
        $mount = $_.Name
        if ($mount -notmatch '^[A-Z]:\\' -and $mountsByVolume -and $mountsByVolume.ContainsKey($_.DeviceID)) {
            # Some volumes without a drive letter are mounted in an NTFS directory.
            $mount = $mountsByVolume[$_.DeviceID] | ForEach-Object { $_.Directory.Name } |
                Where-Object { $_ -match '^[A-Z]:\\' } | Sort-Object | Select-Object -First 1
        }
        # Exclude unmounted system/recovery partitions and inaccessible volumes.
        if ($mount -match '^[A-Z]:\\' -and $_.Capacity -gt 0 -and $null -ne $_.FreeSpace) {
            @{
                mount = $mount
                used = [Math]::Max(0L, [long]$_.Capacity - [long]$_.FreeSpace)
                total = [long]$_.Capacity
            }
        }
    })

    $now = [DateTimeOffset]::UtcNow
    return @{
        platform = 'windows'
        agent_version = 'windows-0.1.0'
        hostname = $env:COMPUTERNAME
        os = $os.Caption.Substring(0, [Math]::Min(128, $os.Caption.Length))
        uptime_seconds = [Math]::Max(0L, [long]($now.UtcDateTime - $os.LastBootUpTime.ToUniversalTime()).TotalSeconds)
        clock_unix = $now.ToUnixTimeSeconds()
        cpu_percent = [Math]::Min(100, [int]$cpu.PercentProcessorTime)
        cpu_cores = [int]$computer.NumberOfLogicalProcessors
        mem_used = $memTotal - $memFree
        mem_total = $memTotal
        mem_free = $memFree
        disks = $disks
    }
}

function Send-Report([uri]$Uri, [string]$Token, [string]$Json) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $request = [Net.HttpWebRequest][Net.WebRequest]::Create($Uri)
    $request.Method = 'POST'
    $request.ContentType = 'application/json; charset=utf-8'
    $request.Headers['Authorization'] = 'Bearer ' + $Token
    $request.AllowAutoRedirect = $false
    $request.Timeout = 15000
    $request.ReadWriteTimeout = 15000
    $bytes = [Text.Encoding]::UTF8.GetBytes($Json)
    $request.ContentLength = $bytes.Length
    try {
        $stream = $request.GetRequestStream()
        try {
            $stream.Write($bytes, 0, $bytes.Length)
        } finally {
            $stream.Dispose()
        }
        $response = $request.GetResponse()
        try {
            $status = [int]$response.StatusCode
        } finally {
            $response.Dispose()
        }
    } catch [Net.WebException] {
        $response = $_.Exception.Response
        if ($null -eq $response) {
            throw "Could not send Windows metrics ($($_.Exception.Status))."
        }
        $status = [int]$response.StatusCode
        $response.Dispose()
    } finally {
        $request.Abort()
    }
    if ($status -ne 200) {
        throw "The panel returned HTTP $status."
    }
}

function Write-ProtectedFile([string]$Path, [byte[]]$Bytes) {
    $tempPath = Join-Path (Split-Path -Parent $Path) ([guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllBytes($tempPath, $Bytes)
        if (Test-Path -LiteralPath $Path) {
            $item = Get-Item -LiteralPath $Path -Force
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'The existing installation contains an unexpected file.'
            }
            # Move with overwrite via Replace, preserving the protected destination ACL.
            [IO.File]::Replace($tempPath, $Path, [NullString]::Value)
        } else {
            [IO.File]::Move($tempPath, $Path)
        }
    } finally {
        [IO.File]::Delete($tempPath)
    }
}

function Install-Monitor([string]$Url, [string]$ScriptPath) {
    $null = Get-ReportUri $Url
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run installation from an elevated Windows PowerShell session.'
    }

    $directory = Join-Path $env:ProgramData 'KervaxMonitor'
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    if (-not (Test-Path -LiteralPath $directory)) {
        $acl = [Security.AccessControl.DirectorySecurity]::new()
        $acl.SetOwner($admins)
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($sid in @($admins, $system)) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new(
                $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'
            )
            $acl.AddAccessRule($rule)
        }
        ([IO.DirectoryInfo]::new($directory)).Create($acl)
    }
    foreach ($item in @(Get-Item -LiteralPath $directory -Force) + @(Get-ChildItem -LiteralPath $directory -Force)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'The installation directory contains a reparse point.'
        }
        $acl = Get-Acl -LiteralPath $item.FullName
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier])
        $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
        if ($owner -notin @($admins, $system, $identity.User) -or
            @($rules | Where-Object { $_.IdentityReference -notin @($admins, $system) }).Count -gt 0) {
            throw 'The installation directory and its files must be accessible only to SYSTEM and Administrators.'
        }
    }
    if (-not (Get-Acl -LiteralPath $directory).AreAccessRulesProtected) {
        throw 'The installation directory must have inheritance disabled.'
    }

    $secret = Read-Host 'Server agent token' -AsSecureString
    $buffer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($buffer)
        if ($token -notmatch '^[A-Za-z0-9._~-]+$') {
            throw 'Enter a valid server agent token.'
        }
        $config = @{ panel_url = $Url; token = $token } | ConvertTo-Json -Compress
        Write-ProtectedFile (Join-Path $directory 'config.json') ([Text.Encoding]::UTF8.GetBytes($config))
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($buffer)
        $secret.Dispose()
    }

    $installed = Join-Path $directory 'windows-monitor.ps1'
    Write-ProtectedFile $installed ([IO.File]::ReadAllBytes($ScriptPath))
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Argument ('-NoProfile -NonInteractive -File "{0}"' -f $installed)
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'Kervax Windows Monitor' -User 'SYSTEM' -Action $action `
        -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Output 'Installed the Kervax Windows Monitor task. It sends a report every minute.'
}

try {
    if ($Install) {
        Install-Monitor $PanelUrl $PSCommandPath
    } elseif ($Preview) {
        New-Report | ConvertTo-Json -Depth 4 -Compress
    } else {
        try {
            $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $uri = Get-ReportUri $config.panel_url
            $token = $config.token
            if ($token -isnot [string] -or $token -notmatch '^[A-Za-z0-9._~-]+$') {
                throw 'Invalid token.'
            }
        } catch {
            throw 'Invalid Windows monitor config. Set panel_url to an HTTPS panel URL and token to the server agent token.'
        }
        $json = New-Report | ConvertTo-Json -Depth 4 -Compress
        Send-Report $uri $token $json
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
