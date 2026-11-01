# Runs inside the WezTerm window. Do not exit the shell on client failure.
$ErrorActionPreference = "Stop"

if ($args.Count -lt 2) {
    Write-Host "Aeon MUD launcher error: missing Python executable or client path." -ForegroundColor Red
    return
}

$pythonPath = [string]$args[0]
$clientPath = [string]$args[1]
$clientArguments = @()
if ($args.Count -gt 2) {
    $clientArguments = @($args[2..($args.Count - 1)])
}

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
