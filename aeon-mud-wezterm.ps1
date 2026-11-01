# Optional GUI launcher. WezTerm is installed separately; Aeon MUD remains stdlib-only.
$ErrorActionPreference = "Stop"

if ($args.Count -eq 0) {
    Write-Host "Usage: .\aeon-mud-wezterm.bat connect --host HOST --port PORT --allow-plaintext"
    Write-Host "       .\aeon-mud-wezterm.bat connect PROFILE"
    exit 2
}

$python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python is required. Install Python and make python.exe available in PATH."
    exit 1
}

# Prefer the GUI-subsystem binary: it does not spawn a second console on Windows.
$potential = @()
if ($env:AEON_WEZTERM_PATH) {
    $potential += $env:AEON_WEZTERM_PATH
}
foreach ($commandName in @("wezterm-gui.exe", "wezterm.exe")) {
    $found = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue
    if ($found) {
        $potential += $found.Source
    }
}
$potential += @(
    (Join-Path $env:ProgramFiles "WezTerm\wezterm-gui.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\WezTerm\wezterm-gui.exe"),
    (Join-Path $env:USERPROFILE "scoop\apps\wezterm\current\wezterm-gui.exe")
)

$wezterm = $null
foreach ($candidate in $potential) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $wezterm = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}
if (-not $wezterm) {
    Write-Host "WezTerm is not installed or could not be found." -ForegroundColor Yellow
    Write-Host "Install: winget install --id wez.wezterm --exact"
    Write-Host "Or set AEON_WEZTERM_PATH to the full path of wezterm-gui.exe."
    exit 1
}

$client = Join-Path $PSScriptRoot "aeon_mud_client.py"
& $wezterm start --cwd $PSScriptRoot -- $python.Source $client @args
if ($LASTEXITCODE -ne $null) {
    exit $LASTEXITCODE
}
