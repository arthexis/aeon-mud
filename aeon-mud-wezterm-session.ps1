# Runs inside the WezTerm window. Do not exit the shell on client failure.
$ErrorActionPreference = "Stop"

$pythonPath = $env:AEON_MUD_PYTHON_PATH
$clientPath = $env:AEON_MUD_SCRIPT_PATH
if (-not $pythonPath -or -not $clientPath) {
    Write-Host "Aeon MUD launcher error: missing Python executable or client path." -ForegroundColor Red
    return
}
$clientArguments = @($args)

Write-Host "Aeon MUD terminal session"
try {
    & $pythonPath $clientPath @clientArguments
    $clientCode = $LASTEXITCODE
    if ($null -eq $clientCode) {
        $clientCode = 1
    }
    if ($clientCode -eq 0) {
        Write-Host "Aeon MUD session ended normally."
    } elseif ($clientCode -eq 130) {
        Write-Host "Aeon MUD interrupted (Ctrl+C)."
    } else {
        Write-Host "Aeon MUD exited with code $clientCode. Review any error above." -ForegroundColor Yellow
    }
} catch {
    Write-Host "Aeon MUD could not start: $_" -ForegroundColor Red
}
Write-Host "This WezTerm window stays open for diagnostics; close the tab/window when done."
