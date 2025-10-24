param(
  [string]$Symbols = "ETH-PERP",
  [double]$OrderUsd = 10,
  [switch]$DryRun,
  [string]$LogLevel = "INFO"
)

# Move to project root
Set-Location -Path (Resolve-Path "$PSScriptRoot\..")

# Activate venv if present
$venv = Join-Path (Get-Location) ".venv\Scripts\Activate.ps1"
if (Test-Path $venv) { . $venv }

$env:LOG_LEVEL = $LogLevel

# Always run on TESTNET. Optionally add --dry-run.
while ($true) {
  $pyArgs = @(
    "-m", "dotenv", "run", "--",
    "python", "run_bot.py", "pfpl",
    "--symbols", $Symbols,
    "--order_usd", $OrderUsd,
    "--testnet"
  )
  if ($DryRun) { $pyArgs += "--dry-run" }

  & python @pyArgs
  if ($LASTEXITCODE -eq 0) { break }
  Start-Sleep -Seconds 5
}

