<#
.SYNOPSIS
  Uploads files to an SSH host and runs one bash script there, logging everything.

.EXAMPLE
  .\Run-Remote.ps1 -Script .\remote\vps_inspect_upgrade.sh -Log ..\..\dist\vps-131-inspect.log
  .\Run-Remote.ps1 -Script .\remote\vps_upgrade.sh -Upload "$env:USERPROFILE\Downloads\open77-server.tar.gz" -ScriptArgs "2.31.21+op77.131"
#>
param(
    [Parameter(Mandatory = $true)] [string] $Script,
    [string[]] $Upload = @(),
    [string] $ScriptArgs = "",
    [string] $Target = $env:F2O77_TARGET,          # user@host (or set F2O77_TARGET)
    [string] $Key = "",
    [string] $Log = ""
)
$ErrorActionPreference = "Continue"
if (-not $Target) { throw "no SSH target: pass -Target user@host or set F2O77_TARGET" }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Log) { $Log = Join-Path $here ("run-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss")) }
if (-not (Test-Path $Script)) { throw "script not found: $Script" }
# powershell -File passes "a","b" as ONE string "a,b": split lists given that way
$Upload = @($Upload | ForEach-Object { $_ -split '[,;]' } | ForEach-Object { $_.Trim().Trim('"') } | Where-Object { $_ })
$candidates = @()
if ($Key) { $candidates += $Key } else {
    $candidates += @($env:F2O77_VPS_KEY, "$env:USERPROFILE\.ssh\id_ed25519", "$env:USERPROFILE\.ssh\id_rsa")
}
$candidates = @($candidates | Where-Object { $_ -and (Test-Path $_) })
if (-not $candidates) { throw "no SSH key found (pass -Key or set F2O77_VPS_KEY)" }
"[$(Get-Date -Format s)] $(Split-Path -Leaf $Script) $ScriptArgs -> $Target" | Tee-Object -FilePath $Log
$remote = "/tmp/f2o77-run"
$opts = $null
foreach ($k in $candidates) {
    $try = @("-i", $k, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
             "-o", "ServerAliveInterval=20")
    & ssh @try $Target "mkdir -p $remote && chmod 700 $remote" 2>$null
    if ($LASTEXITCODE -eq 0) { $opts = $try; "key: $(Split-Path -Leaf $k)" | Tee-Object -FilePath $Log -Append; break }
    "key $(Split-Path -Leaf $k) refused ($LASTEXITCODE)" | Tee-Object -FilePath $Log -Append
}
if (-not $opts) { throw "ssh to $Target failed with every key tried" }
foreach ($f in @($Script) + $Upload) {
    if (-not (Test-Path $f)) { throw "file not found: $f" }
    "upload $(Split-Path -Leaf $f) ($([math]::Round((Get-Item $f).Length / 1MB, 1)) MiB)" | Tee-Object -FilePath $Log -Append
    & scp @opts $f "${Target}:$remote/"
    if ($LASTEXITCODE -ne 0) { throw "scp of $f failed ($LASTEXITCODE)" }
}
$name = Split-Path -Leaf $Script
& ssh @opts $Target "cd $remote && bash ./$name $ScriptArgs" 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $Log -Append
$rc = $LASTEXITCODE
"[$(Get-Date -Format s)] exit $rc" | Tee-Object -FilePath $Log -Append
exit $rc
