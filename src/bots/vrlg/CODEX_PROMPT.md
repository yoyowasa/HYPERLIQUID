# VRLG（Validator Rotation Liquidity‑Gap）戦略 — 実装依頼プロンプト（Codex用）

## あなた（Codex）への役割
- あなたは **HYPERLIQUID** リポジトリ内で、**VRLG ボット**を安全・高速・再利用可能な形で実装するエンジニア相方です。
- 既存の **PFPL** ボットと **共通化**できる部品は **src/hl_core/** に置き、両者で再利用します。

## 出力スタイル（厳守）
- **「置き替えるコード／新規に必要なコードだけ」をコードブロックで出力**してください。
- **diff 形式は禁止**。`+/-` や差分記号は使わないでください。
- **行番号は絶対に出力しない**でください。
- どの箇所を直すかは、**「ファイル名」と「検索キーワード」**で指示してください（例：「`src/bots/vrlg/signal_detector.py` の `def check_signal(` を検索して、その直後に以下を追記」）。
- **説明文はコードブロックの外**に短く添えてください。コードブロック内は **コードだけ**。
- **1 ステップに 1 項目のみ**を出力。私が「次」と送るまで **次の手順を絶対に出さない**でください。

## リポジトリ前提と共通化ポリシー
- ルート構成：`src/hl_core/`（共通基盤）, `src/bots/`（各ボット）, `tests/`, `.github/workflows/` など。
- **共通化する主対象**
  - `hl_core/api/`：HTTP/WS クライアントと型（PFPL と共通）
  - `hl_core/utils/logger.py`：色付きログ／日次ローテート／Discord 連携（PFPL と共通）
  - `hl_core/utils/config.py`：`YAML` / `.env` 読み出し・バリデーション（PFPL と共通）
  - `hl_core/utils/backtest.py`：共通シミュレーター基盤（VRLG 用に拡張フックを追加）
- **VRLG 専用**のロジックは `src/bots/vrlg/` に置きます。

## 目標（クイックサマリー）
- 狙い：**ブロック提案者交代**（境界）周辺で生じる一瞬の板薄・スプレッド拡大を、**post‑only Iceberg 指値**で刈り取り、**1–3 ticks** の微利を高回転で積む。
- 時間軸：ホールド **0.3–3 秒**、1 トレード利幅 **4–10 bps** 目安。
- 勝ち筋：① **遅延最小化**、② **成行解消の滑り抑制**、③ **Kill‑switch** 厳格運用。
- 相関：MES/OPDH とやや高相関、PFPL とは独立。

## 性能・遅延バジェット（必達）
- **WS→特徴量更新 ≤ 5ms**
- **Signal→注文発行 ≤ 200µs**（プロセス内キュー）
- ingest 遅延デフォルト：`10–30ms`、注文RTT：`30–80ms`（コンフィグ化）

## ファイル配置（作成/更新予定）
- `src/bots/vrlg/data_feed.py`：WS購読と100ms特徴量集計
- `src/bots/vrlg/rotation_detector.py`：自己相関で周期 R* 推定＋p値ゲート
- `src/bots/vrlg/signal_detector.py`：4 条件シグナル（phase/DoB/spread/OBI）
- `src/bots/vrlg/execution_engine.py`：postOnly Iceberg／TTL／OCO／IOC
- `src/bots/vrlg/risk_management.py`：Kill‑switch／露出・滑り・板消費率／クールダウン
- `src/bots/vrlg/config.py`：設定モデル（`configs/strategy.toml` を読む）
- `backtest/vrlg_sim.py`：100ms 足・部分成行・キャンセル近似＋レイテンシ注入
- `monitoring/vrlg.json`：Grafana（Prometheus 指標の可視化）
- `configs/strategy.toml`：VRLG 用のデフォルト値（PFPL と同形式）

## 仕様（データ・派生指標）
- フィード：`blocks`（≤100ms）, `level2`（≤50ms）, `trades`（≤50ms 任意）
- 100ms 足で更新
  - `mid_t = (bestBid + bestAsk) / 2`
  - `spread_ticks_t = (bestAsk - bestBid) / tick_size`
  - `DoB_t = bidSize_L1 + askSize_L1`
  - `OBI_t = (bidSize_L1 - askSize_L1) / (bidSize_L1 + askSize_L1)`
  - `block_phase_t`：R* から位相 0–1
- 遅延バジェット遵守のため、**ゼロアロケーション志向**（事前確保・再利用）で実装。

## 周期検出（ローテーション相当）
- 直近 `T_roll = 30s` の `DoB_t` と `spread_ticks_t` に自己相関を取り、最強周期を `R*` とする。
- `block_phase_t = (t mod R*) / R*` とし、**位相 0/1 近傍で DoB↓・spread↑** が **200 回以上**で再現されるか p 値で確認（例：境界窓 vs 非境界窓の Welch t 検定）。
- **p 値 ≤ 0.01** を満たすまで **稼働禁止（様子見モード）**。

## シグナル条件（同時成立）
- 前提パラメータ：`N=80, x=0.25, y=2, z=0.6`
- `phase_gate = block_phase_t ∈ {0±z} ∪ {1±z}`
- `DoB_t < median(DoB_{t−N:t}) × (1−x)`
- `spread_ticks_t ≥ y`
- `|OBI_t| ≤ 0.6`
- 成立で **Signal = True**（キューへ publish）

## 執行（エントリー／エグジット／損切り）
- 方式：**postOnly + Iceberg**
- 価格：`mid_t ± 0.5 tick`（両面 or 片面優先）
- 表示数量：`display = min(max(total×0.25, 0.01 BTC), total)`
- TTL：`1.0s`（埋まらなければキャンセル）
- サイズ：口座残高 `0.2–0.5%` を小刻みに分割
- 解消：fill 後 ≤ `500ms` で **IOC**。代替：`spread_ticks_t == 1` で片面成行。
- OCO：`mid_t ± 3 ticks` で逆指値／Time‑Stop `1.2s`

## リスク管理（ルールベース）
- ブロック間隔 p50 `> 4s`：即 **Flat** ＆戦略停止
- 板消費率（自分の発注/TopDepth）`> 2%/5s`：サイズ 50% 減
- 滑り（実 fill − mid）平均 `> 1 tick/1m`：postOnly を **深め**に／成行禁止
- 連続損切り `3 回/10m`：一時停止（10m）
- 相関監視：Aバケツ総Δが `VaR₁ₛ × 0.8` 超でヘッジ
- クールダウン：同方向 fill 後 `2×R* 秒` 新規禁止

## バックテストとゲート
- `scripts/record_ws.py`：L2/blocks を **parquet** 録画
- `backtest/vrlg_sim.py`：100ms 足／先頭優先／部分成行／キャンセル近似、遅延注入（ingest 10–30ms, order RTT 30–80ms）
- 指標：HitRate, Avg PnL/trade, Slip, Holding(ms), Trades/min, MaxDD
- スイープ（optuna）：`x∈{0.15,0.2,0.25,0.3}, y∈{1,2,3}, TTL∈{0.6,1.0,1.4}s`
- 目的関数：`Sharpe – 0.5×MaxDD – 0.1×|Δ|平均`
- Paper 48h のゲート：滑り `< 0.7 tick`, 勝率 `≥ 55%`, 週次 `MaxDD < 日利幅×8`

## Prometheus / Grafana
- Export: `vrlg_signal_count`, `fill_rate`, `slippage_ticks`, `block_interval_ms`, `orders_rejected` など。
- `monitoring/vrlg.json` にダッシュボード定義。

## コンフィグ例（`configs/strategy.toml`）
```toml
[symbol]
name      = "BTCUSD-PERP"
tick_size = 0.5

[signal]
N   = 80
x   = 0.25
y   = 2
z   = 0.6

[exec]
order_ttl_ms     = 1000
display_ratio    = 0.25
min_display_btc  = 0.01
max_exposure_btc = 0.8
cooldown_factor  = 2.0

[risk]
max_slippage_ticks = 1
max_book_impact    = 0.02
time_stop_ms       = 1200
stop_ticks         = 3

[latency]
ingest_ms   = 10
order_rt_ms = 60
```

インターフェイス仕様（関数ごとの役割を明記）

ここは 実装すべき関数の責務をはっきり示します。実装時は各関数に docstring を必ず付けてください。

# src/bots/vrlg/data_feed.py
# 目的: WSからblocks/level2を購読し、100msごとに特徴量(mid, spread_ticks, DoB, OBI)を更新・配信する。
async def subscribe_blocks() -> "AsyncIterator[BlockMeta]": ...
async def subscribe_l2(symbol: str) -> "AsyncIterator[TopOfBook]": ...
async def feature_aggregator(...) -> "AsyncIterator[Features100ms]": ...

# src/bots/vrlg/rotation_detector.py
# 目的: 自己相関でR*を推定し、位相と有意性(p値)を計算。p<=0.01で稼働許可、超過で様子見。
def estimate_period(dob: "Deque[float]", spr: "Deque[float]", roll_s: float = 30.0) -> "PeriodEst":
    ...
def compute_phase(now_ts: float, r_star: float) -> float: ...
def boundary_significance_test(...) -> "PValueResult": ...

# src/bots/vrlg/signal_detector.py
# 目的: 位相ゲート, DoB薄, spread閾, OBIバランスを同時満たす時にTrueを返す。
def check_signal(feat: "Features100ms", med: "RollingMedian", cfg: "SignalCfg") -> bool: ...

# src/bots/vrlg/execution_engine.py
# 目的: postOnly Icebergの発注/キャンセル/TTL/OCO/IOCを一元実装。滑りと板消費を記録。
async def post_only_iceberg(side: str, price: float, total: float, display: float, ttl_ms: int, symbol: str) -> str: ...
async def place_oco_stop(mid: float, stop_ticks: int, symbol: str) -> tuple[str, str]: ...
async def wait_fill_or_ttl(order_ids: list[str], timeout_ms: int) -> "FillResult": ...
async def close_all_by_ioc(symbol: str) -> None: ...

# src/bots/vrlg/risk_management.py
# 目的: Kill-switchや露出/滑り/板消費/クールダウンを評価し、取引可否を返す。
def should_pause(block_interval_p50_s: float) -> bool: ...
def adjust_size_by_impact(book_impact_5s: float, base_size: float) -> float: ...
def record_slippage(slippage_ticks_1m_avg: float) -> None: ...
def cooldown_guard(last_fill_ts: float, r_star: float, factor: float) -> bool: ...

# src/bots/vrlg/config.py
# 目的: configs/strategy.toml を読み込み、型安全に各モジュールへ供給する。
class StrategyConfig(BaseModel): ...
def load_config(path: str = "configs/strategy.toml") -> StrategyConfig: ...

# backtest/vrlg_sim.py
# 目的: 100ms足のイベント駆動型シミュレータ。遅延注入・部分約定・キャンセル再現、指標を出力。
def run_sim(record_path: str, cfg_path: str) -> "SimReport": ...

テスト

tests/unit/：各関数の単体

tests/integration/：bots/vrlg/strategy の流れ

tests/e2e/：Live Key がある場合のみ

最初に実行するステップ（あなたの次の出力）

「ステップ1：足場づくり」

src/bots/vrlg/ に各 .py の 空ファイル（最小の関数スタブとdocstringのみ） を新規追加。

configs/strategy.toml に VRLGのデフォルトセクション を追加（PFPLの形式を踏襲）。

hl_core/utils/backtest.py に VRLG用の拡張フック を小さく追加（既存APIは壊さない）。
