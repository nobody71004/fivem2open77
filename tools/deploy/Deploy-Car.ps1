<#
.SYNOPSIS
  Pushes converted cars (a bundle made by make_bundle.sh) to Open77 servers over SSH.

.DESCRIPTION
  Copies the bundle and server_deploy.sh to the host and runs it there for each server:
    -Mode inspect   read-only: Open77 build, players online, requiredMods, what is installed
    -Mode deploy    back up, install, add to resources.load, --check-config; with -Restart and
                    nobody online, restart and verify the log (listener, required mods, resources)
  Uses your own SSH key; nothing is stored anywhere else.  The full output also goes to -Log.

.EXAMPLE
  .\Deploy-Car.ps1 -Bundle ..\..\dist\i8rpncs-bundle.tgz -Mode inspect
  .\Deploy-Car.ps1 -Bundle ..\..\dist\i8rpncs-bundle.tgz -Mode deploy -Restart
#>
param(
    [Parameter(Mandatory = $true)] [string] $Bundle,
    [ValidateSet("inspect", "deploy")] [string] $Mode = "inspect",
    [string] $Target = $env:F2O77_TARGET,          # user@host (or set F2O77_TARGET)
    [string] $Key = "",
    [string[]] $Servers = @("open77-xbuniverse", "open77-xbuniverse-staging"),
    [switch] $Restart,
    [switch] $AllowUnsecured,
    [string] $Log = ""
)
# Native ssh/scp report through exit codes; "Stop" would turn their stderr chatter into errors
# under Windows PowerShell 5.1, so failures are checked explicitly below.
$ErrorActionPreference = "Continue"
if (-not $Target) { throw "no SSH target: pass -Target user@host or set F2O77_TARGET" }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Log) { $Log = Join-Path $here ("deploy-{0}-{1}.log" -f $Mode, (Get-Date -Format "yyyyMMdd-HHmmss")) }
if (-not (Test-Path $Bundle)) { throw "bundle not found: $Bundle" }
# Which key the host accepts: -Key, else $env:F2O77_VPS_KEY, else the keys this PC has used for it.
$candidates = @()
if ($Key) { $candidates += $Key } else {
    $candidates += @($env:F2O77_VPS_KEY, "$env:USERPROFILE\.ssh\id_ed25519", "$env:USERPROFILE\.ssh\id_rsa")
}
$candidates = @($candidates | Where-Object { $_ -and (Test-Path $_) })
if (-not $candidates) { throw "no SSH key found (pass -Key or set F2O77_VPS_KEY)" }
"[$(Get-Date -Format s)] $Mode -> $Target : $($Servers -join ', ')" | Tee-Object -FilePath $Log
$remote = "/tmp/f2o77-deploy"
$opts = $null
foreach ($k in $candidates) {
    $try = @("-i", $k, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
    & ssh @try $Target "mkdir -p $remote && chmod 700 $remote" 2>$null
    if ($LASTEXITCODE -eq 0) { $opts = $try; "key: $(Split-Path -Leaf $k)" | Tee-Object -FilePath $Log -Append; break }
    "key $(Split-Path -Leaf $k) refused ($LASTEXITCODE)" | Tee-Object -FilePath $Log -Append
}
if (-not $opts) { throw "ssh to $Target failed with every key tried" }
& scp @opts $Bundle "${Target}:$remote/bundle.tgz"
if ($LASTEXITCODE -ne 0) { throw "scp of the bundle failed ($LASTEXITCODE)" }
& scp @opts (Join-Path $here "server_deploy.sh") "${Target}:$remote/server_deploy.sh"
if ($LASTEXITCODE -ne 0) { throw "scp of server_deploy.sh failed ($LASTEXITCODE)" }
$flags = @()
if ($Restart) { $flags += "--restart" }
if ($AllowUnsecured) { $flags += "--allow-unsecured" }
$cmd = "bash $remote/server_deploy.sh $Mode $remote/bundle.tgz $($Servers -join ' ') $($flags -join ' ')"
& ssh @opts $Target $cmd 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $Log -Append
$rc = $LASTEXITCODE
"[$(Get-Date -Format s)] exit $rc" | Tee-Object -FilePath $Log -Append
exit $rc
