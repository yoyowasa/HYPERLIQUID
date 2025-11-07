# PFPL を本番起動し、異常終了時は 5 秒後に自動再起動する PowerShell スクリプト
param(
  [string]$Symbols = "ETH-PERP",
  [double]$OrderUsd = 10,
  [switch]$Debug
)

# リポジトリ ルートに移動（このスクリプトは scripts/ 配下に置く前提）
Set-Location -Path (Resolve-Path "$PSScriptRoot\..")

# 仮想環境を有効化（存在すれば）
$venv = Join-Path (Get-Location) ".venv\Scripts\Activate.ps1"
if (Test-Path $venv) { . $venv }

# 本番フラグとログレベル（任意）を設定
$env:GO = "live"
if ($Debug) { $env:LOG_LEVEL = "DEBUG" }

# .env を読み込みつつ PFPL を実行。終了コード≠0 のとき 5 秒待って再起動
while ($true) {
  python run_bot.py pfpl --symbols $Symbols --order_usd $OrderUsd
  if ($LASTEXITCODE -eq 0) { break }
  Start-Sleep -Seconds 5
}
