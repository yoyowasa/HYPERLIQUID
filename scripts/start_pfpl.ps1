# PFPL を本番起動し、異常終了時は 5 秒後に自動再起動する PowerShell スクリプト
param(
  [string]$Symbols = "ETH-PERP",
  [double]$OrderUsd = 10,
  [string]$PairCfg = "",
  [switch]$Debug
)

# リポジトリ ルートに移動（このスクリプトは scripts/ 配下に置く前提）
Set-Location -Path (Resolve-Path "$PSScriptRoot\..")

# 役割: PowerShell で \"foo\" と書くと \foo\ が渡るため、その救済を行う
function Normalize-Arg([string]$s) {
  if (-not $s) { return $s }
  $t = $s.Trim()
  # 例: \"ETH-PERP\" -> \ETH-PERP\ になるケースを 1 文字だけ剥がす
  if (
    $t.Length -ge 2 -and
    $t.StartsWith('\') -and
    $t.EndsWith('\') -and
    -not ($t.StartsWith('\\')) -and
    -not ($t -match '^[A-Za-z]:\\')
  ) {
    $t = $t.Substring(1, $t.Length - 2)
  }
  return $t.Trim('"').Trim("'")
}

$Symbols = Normalize-Arg $Symbols
$PairCfg = Normalize-Arg $PairCfg

# pair_cfg は存在チェックして、存在するなら絶対パスに正規化する（無限リトライ防止）
if ($PairCfg) {
  if (-not (Test-Path $PairCfg)) {
    Write-Host "[start_pfpl] PairCfg not found: $PairCfg (cwd=$(Get-Location))"
    exit 2
  }
  try { $PairCfg = (Resolve-Path $PairCfg).Path } catch { }
}

# 仮想環境を有効化（存在すれば）
$venv = Join-Path (Get-Location) ".venv\Scripts\Activate.ps1"
if (Test-Path $venv) { . $venv }

# 本番フラグとログレベル（任意）を設定
$env:GO = "live"
if ($Debug) { $env:LOG_LEVEL = "DEBUG" }

# .env を読み込みつつ PFPL を実行。落ちたら 5 秒待って再起動（実稼働向け）
$restart = 0
while ($true) {
  $restart += 1
  $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Write-Host "[start_pfpl] start#$restart $ts symbols=$Symbols order_usd=$OrderUsd pair_cfg=$PairCfg"

  $pyArgs = @(
    "run_bot.py", "pfpl",
    "--symbols", $Symbols,
    "--order_usd", $OrderUsd
  )
  if ($PairCfg) { $pyArgs += @("--pair_cfg", $PairCfg) }

  & python @pyArgs
  $ts2 = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Write-Host "[start_pfpl] exit_code=$LASTEXITCODE $ts2"
  # Ctrl+C は停止（Windows では 130 になることが多い）
  if ($LASTEXITCODE -eq 130) { break }
  Start-Sleep -Seconds 5
}
