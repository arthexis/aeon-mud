# Optional GUI launcher. The installed wezterm.exe CLI delegates to the GUI process.
$ErrorActionPreference = "Stop"

if ($args.Count -eq 0) {
    Write-Host "Usage: .\aeon-mud-wezterm.bat connect --host HOST --port PORT --allow-plaintext"
    Write-Host "       .\aeon-mud-wezterm.bat connect PROFILE"
    exit 2
}

# Get-Command can return multiple installations; select exactly one executable.
$python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $python) {
    Write-Host "Python is required. Install Python and make python.exe available in PATH." -ForegroundColor Red
    exit 1
}

# CLI executables surface errors in the caller's console. The GUI binary doesn't.
# Prefer the CLI even when only the GUI binary was discovered on PATH.
$potential = @()
if ($env:AEON_WEZTERM_PATH) {
    $potential += $env:AEON_WEZTERM_PATH
}
$cliCommand = Get-Command wezterm.exe -CommandType Application -ErrorAction SilentlyContinue
if ($cliCommand) {
    $potential += $cliCommand.Source
}
$locations = @()
if ($env:ProgramFiles) {
    $locations += (Join-Path $env:ProgramFiles "WezTerm\wezterm.exe")
}
if ($env:LOCALAPPDATA) {
    $locations += (Join-Path $env:LOCALAPPDATA "Programs\WezTerm\wezterm.exe")
}
if ($env:USERPROFILE) {
    $locations += (Join-Path $env:USERPROFILE "scoop\apps\wezterm\current\wezterm.exe")
}
$potential += $locations
$guiCommand = Get-Command wezterm-gui.exe -CommandType Application -ErrorAction SilentlyContinue
if ($guiCommand) {
    $potential += $guiCommand.Source
}

$wezterm = $null
foreach ($candidate in $potential) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        continue
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    if ([System.IO.Path]::GetFileName($resolved) -ieq "wezterm-gui.exe") {
        $siblingCli = Join-Path (Split-Path -Parent $resolved) "wezterm.exe"
        if (Test-Path -LiteralPath $siblingCli -PathType Leaf) {
            $resolved = $siblingCli
        }
    }
    $wezterm = $resolved
    break
}
if (-not $wezterm) {
    Write-Host "WezTerm was not found." -ForegroundColor Yellow
    Write-Host "Install: winget install --id wez.wezterm --exact"
    Write-Host "If just installed, open a new PowerShell window or set AEON_WEZTERM_PATH."
    exit 1
}

$client = Join-Path $PSScriptRoot "aeon_mud_client.py"
if (-not (Test-Path -LiteralPath $client -PathType Leaf)) {
    Write-Host "Aeon MUD client not found: $client" -ForegroundColor Red
    exit 1
}

$sessionScript = Join-Path $PSScriptRoot "aeon-mud-wezterm-session.ps1"
if (-not (Test-Path -LiteralPath $sessionScript -PathType Leaf)) {
    Write-Host "Aeon MUD session wrapper not found: $sessionScript" -ForegroundColor Red
    exit 1
}

# Set paths in the child environment rather than relying on Windows -File
# positional argument binding. The remaining arguments are all client options.
$env:AEON_MUD_PYTHON_PATH = $python.Source
$env:AEON_MUD_SCRIPT_PATH = $client
Write-Host "Launching Aeon MUD in WezTerm..."
Write-Host "WezTerm: $wezterm"
try {
    # -NoExit keeps the pane open even when Python exits before displaying its error.
    & $wezterm start --cwd $PSScriptRoot -- powershell.exe -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File $sessionScript @args
    $launchCode = $LASTEXITCODE
} catch {
    Write-Host "WezTerm launch failed: $_" -ForegroundColor Red
    exit 1
}
if ($launchCode -ne 0) {
    Write-Host "WezTerm exited with code $launchCode. Try running '$wezterm start --help'." -ForegroundColor Red
    if ($null -eq $launchCode) { exit 1 }
    exit $launchCode
}
Write-Host "WezTerm launch command completed. If no window opened, test 'wezterm.exe start' directly."
