$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "aeon_mud_client.py"
& python $script @args
exit $LASTEXITCODE
