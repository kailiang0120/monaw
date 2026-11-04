param(
    # Installer of the release users have now.
    [string]$FromInstaller,
    # Installer of the new build.
    [string]$ToInstaller,
    # Instead of installing, check that data survives a restart of an app folder
    # (for example frontend\release\win-unpacked). Changes nothing outside a temporary folder.
    [string]$AppDir,
    # Where the Monaw home, install folder and logs go; a new temporary folder by default.
    [string]$WorkDir,
    [string]$ExpectedVersion,
    [string]$ExpectedCandidateSha256
)

# Upgrade check: install $FromInstaller silently, seed settings, a conversation
# and a scheduled task, install $ToInstaller over it as a user would, then check
# the new version starts and kept the data, and that uninstalling keeps the
# Monaw home. Run it on a clean machine: it refuses where Monaw is installed,
# because the installers share one uninstall entry. See docs/packaging.md.

$ErrorActionPreference = "Stop"

if (-not $AppDir -and (-not $FromInstaller -or -not $ToInstaller)) {
    throw "Pass -FromInstaller and -ToInstaller, or -AppDir."
}
if (-not $WorkDir) {
    $WorkDir = Join-Path ([IO.Path]::GetTempPath()) "monaw-upgrade-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
}
$home_ = Join-Path $WorkDir "home"
$installDir = Join-Path $WorkDir "Monaw Install"
New-Item -ItemType Directory -Force $home_ | Out-Null

$secret = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
$marker = "upgrade-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$workspace_ = Join-Path $WorkDir "workspace"
New-Item -ItemType Directory -Force $workspace_ | Out-Null

function Get-InstalledMonaw {
    $keys = @(
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "Monaw*" }
}

function Get-FreePort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    return $port
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds, [string]$What) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "Timed out after $Seconds s waiting for $What."
}

function Test-Health([int]$Port) {
    try {
        return (Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Stop-AppProcesses([string]$Folder) {
    Get-CimInstance Win32_Process -Filter "Name='Monaw.exe' OR Name='monaw-backend.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Folder, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Wait-Until {
        -not (Get-CimInstance Win32_Process -Filter "Name='Monaw.exe' OR Name='monaw-backend.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Folder, [StringComparison]::OrdinalIgnoreCase) })
    } 30 "Monaw processes in $Folder to exit"
}

# The frozen backend on its own, with a development token, so the check can use the API.
function Start-Backend([string]$Folder) {
    $port = Get-FreePort
    $env:MONAW_HOME = $home_
    $env:MONAW_CONTROL_SECRET = $secret
    $env:MONAW_ALLOW_DEVELOPMENT_TOKEN = "1"
    $log = Join-Path $WorkDir "backend-$port.log"
    try {
        $process = Start-Process (Join-Path $Folder "resources\backend\monaw-backend.exe") `
            -ArgumentList "--host", "127.0.0.1", "--port", $port -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    }
    finally {
        Remove-Item Env:MONAW_CONTROL_SECRET, Env:MONAW_ALLOW_DEVELOPMENT_TOKEN, Env:MONAW_HOME -ErrorAction SilentlyContinue
    }
    Wait-Until { Test-Health $port } 60 "the backend in $Folder to serve /health"
    return @{ Port = $port; Process = $process }
}

function Stop-Backend($Backend) {
    Stop-Process -Id $Backend.Process.Id -Force -ErrorAction SilentlyContinue
    Wait-Until { -not (Test-Health $Backend.Port) } 20 "the backend to stop"
}

function Invoke-Api($Backend, [string]$Method, [string]$Path, $Body) {
    $request = @{
        Uri         = "http://127.0.0.1:$($Backend.Port)$Path"
        Method      = $Method
        Headers     = @{ Authorization = "Bearer $secret" }
        ContentType = "application/json"
    }
    if ($null -ne $Body) { $request.Body = ($Body | ConvertTo-Json -Depth 8) }
    return Invoke-RestMethod @request
}

function Add-Data($Backend) {
    Invoke-Api $Backend PUT "/api/settings" @{ identity = @{ user_name = $marker } } | Out-Null
    $script:conversation = Invoke-Api $Backend POST "/api/conversations" @{ title = "Conversation $marker"; workspace_dir = $workspace_ }
    Invoke-Api $Backend POST "/api/scheduled-tasks" @{
        title = "Task $marker"; prompt = "Upgrade check; never runs."; scheduleKind = "cron"; cronExpr = "0 9 * * 1"; enabled = $false
    } | Out-Null
    Write-Host "Seeded settings, a conversation and a scheduled task ($marker)."
}

function Assert-Data($Backend) {
    $failures = @()
    if ((Invoke-Api $Backend GET "/api/settings").identity.user_name -ne $marker) { $failures += "settings" }
    if (-not ((Invoke-Api $Backend GET "/api/conversations") | Where-Object { $_.id -eq $conversation.id -and $_.workspace_dir -eq $workspace_ })) { $failures += "conversation or workspace" }
    if (-not ((Invoke-Api $Backend GET "/api/conversations/$($conversation.id)/messages").messages | Where-Object { $_.role -eq "user" -and $_.content -eq "Message $marker" })) { $failures += "history message" }
    if (-not ((Invoke-Api $Backend GET "/api/scheduled-tasks") | Where-Object { $_.title -eq "Task $marker" -and $_.enabled -eq $false })) { $failures += "disabled scheduled task" }
    if ($failures) { throw "Missing after the upgrade: $($failures -join ', ')." }
    Write-Host "Settings, conversation and scheduled task are intact."
}

function Add-History {
    # Seed history in the old schema while its backend is stopped. No model account is needed.
    $code = 'import sqlite3,sys,datetime; c=sqlite3.connect(sys.argv[1]); c.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)", (sys.argv[2],"user",sys.argv[3],datetime.datetime.now(datetime.timezone.utc).isoformat())); c.commit()'
    uv run --directory backend --locked python -c $code (Join-Path $home_ "runtime\agent.db") $conversation.id "Message $marker"
    if ($LASTEXITCODE -ne 0) { throw "Failed to seed historical message." }
}

# The whole app, as a user starts it, until its backend is healthy.
function Test-AppStarts([string]$Folder) {
    $port = Get-FreePort
    # Electron's own data goes to the work folder too, not the user's AppData.
    $profile_ = Join-Path $WorkDir "profile"
    $appEnv = @{
        MONAW_HOME = $home_; MONAW_BACKEND_PORT = "$port"; USERPROFILE = $profile_
        APPDATA = Join-Path $profile_ "AppData\Roaming"; LOCALAPPDATA = Join-Path $profile_ "AppData\Local"
    }
    New-Item -ItemType Directory -Force $appEnv.APPDATA, $appEnv.LOCALAPPDATA | Out-Null
    $saved = @{}
    foreach ($name in $appEnv.Keys) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name)
        [Environment]::SetEnvironmentVariable($name, $appEnv[$name])
    }
    try {
        Start-Process (Join-Path $Folder "Monaw.exe") -WindowStyle Hidden | Out-Null
    }
    finally {
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
    }
    Wait-Until { Test-Health $port } 90 "Monaw.exe in $Folder to start its backend"
    Stop-AppProcesses $Folder
    Write-Host "Monaw.exe started and its backend became healthy."
}

function Install-Monaw([string]$Installer) {
    $started = Get-Date
    # NSIS: /D must be last and unquoted.
    $process = Start-Process $Installer -ArgumentList "/S", "/D=$installDir" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "$Installer exited with $($process.ExitCode)." }
    Wait-Until { Test-Path (Join-Path $installDir "Monaw.exe") } 60 "Monaw.exe in the install folder"
    Write-Host ("Installed {0} in {1:n0} s." -f (Split-Path $Installer -Leaf), ((Get-Date) - $started).TotalSeconds)
}

function Uninstall-Monaw {
    $uninstaller = Join-Path $installDir "Uninstall Monaw.exe"
    if (-not (Test-Path $uninstaller)) { return }
    Start-Process $uninstaller -ArgumentList "/S" -Wait | Out-Null
    # The uninstaller copies itself elsewhere and returns before it finishes.
    Wait-Until { -not (Test-Path (Join-Path $installDir "Monaw.exe")) } 60 "the uninstaller to remove Monaw.exe"
    Wait-Until { -not (Get-InstalledMonaw) } 30 "the uninstall entry to go"
}

Write-Host "Work folder: $WorkDir"

if ($AppDir) {
    $AppDir = (Resolve-Path $AppDir).Path
    $backend = Start-Backend $AppDir
    Add-Data $backend
    Stop-Backend $backend
    Add-History
    $backend = Start-Backend $AppDir
    try { Assert-Data $backend } finally { Stop-Backend $backend }
    Test-AppStarts $AppDir
    Write-Host "Data survives a restart of $AppDir."
    exit 0
}

$FromInstaller = (Resolve-Path $FromInstaller).Path
$ToInstaller = (Resolve-Path $ToInstaller).Path
Write-Host "Source installer SHA-256: $((Get-FileHash $FromInstaller -Algorithm SHA256).Hash)"
Write-Host "Candidate installer SHA-256: $((Get-FileHash $ToInstaller -Algorithm SHA256).Hash)"
if ($ExpectedCandidateSha256 -and (Get-FileHash $ToInstaller -Algorithm SHA256).Hash -ne $ExpectedCandidateSha256) {
    throw "Candidate installer hash mismatch."
}
if (Get-InstalledMonaw) {
    throw "Monaw is installed on this machine. Run the upgrade check on a clean machine, such as the Upgrade check workflow."
}

try {
    Install-Monaw $FromInstaller
    $backend = Start-Backend $installDir
    Add-Data $backend
    Stop-Backend $backend
    Add-History
    Test-AppStarts $installDir
    $oldAsar = (Get-FileHash (Join-Path $installDir "resources\app.asar")).Hash

    Install-Monaw $ToInstaller
    if ((Get-FileHash (Join-Path $installDir "resources\app.asar")).Hash -eq $oldAsar) {
        throw "resources\app.asar is unchanged; the two installers may contain the same app."
    }
    if ($ExpectedVersion) {
        $installed = @(Get-InstalledMonaw)
        if ($installed.Count -ne 1 -or $installed[0].DisplayVersion -ne $ExpectedVersion) {
            throw "Installed version is not $ExpectedVersion."
        }
        Write-Host "Installed version: $($installed[0].DisplayVersion)"
    }
    if (-not (Test-Path (Join-Path $installDir "THIRD_PARTY_NOTICES.txt"))) {
        throw "THIRD_PARTY_NOTICES.txt is missing from the upgraded install."
    }
    $backend = Start-Backend $installDir
    try { Assert-Data $backend } finally { Stop-Backend $backend }
    Test-AppStarts $installDir

    Uninstall-Monaw
    if (-not (Test-Path (Join-Path $home_ "runtime"))) { throw "Uninstalling removed the Monaw home." }
    Write-Host "Uninstalling kept the Monaw home."
    Write-Host "Upgrade check passed."
}
finally {
    Stop-AppProcesses $installDir
    Uninstall-Monaw
}
