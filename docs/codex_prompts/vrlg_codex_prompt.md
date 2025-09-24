# VRLG (Validator Rotation Liquidity‑Gap) — Codex Implementation Prompt

You are **Codex**. Implement a high‑frequency micro‑scalping bot named **VRLG** inside the **HYPERLIQUID** repository.  
Target language: **Python 3.12+**. Runtime: **asyncio + uvloop**. Style: **type‑hinted, ruff/black/mypy clean, docstrings first**.  
**Do not invent APIs**: where actual exchange endpoints are unknown, create thin adapters that call **`hl_core.api`** placeholders and keep all HTTP/WS details behind interfaces.

> **Repository constraints (must follow):**  
> - Reuse the shared foundation in `src/hl_core/` (logger, config, backtest, API wrappers, infra).  
> - Put the new bot under `src/bots/vrlg/`. PFPL (`src/bots/pfpl/`) already exists and VRLG must **share** reusable code/infra rather than duplicating (logger/config/backtest/metrics).  
> - Keep tests in `tests/{unit,integration,e2e}`. Wire CI the same way as PFPL.  
> - One source of truth for configuration via `hl_core.utils.config`.

---

## 0) Goal & Trading Summary (1 minute brief)

- **Objective**: Exploit **momentary spread widening & top‑of‑book thinning** around **block proposer rotation** events on Hyperliquid by placing **post‑only Iceberg** limit orders near mid, harvesting **1–3 ticks** repeatedly.  
- **Holding time**: 0.3–3 s, **PnL/trade** target: **4–10 bps (net)**.  
- **Edge**: (i) minimal signal latency, (ii) controlled unwind slippage, (iii) strict kill‑switch.  
- **Correlation**: Medium with MES/OPDH (same "sub‑second orderbook gap" family), independent of PFPL.

---

## 1) Deliverables (files to create)

Create the following **modules and assets**. Prefer composition over inheritance; still, if `BaseStrategy` (PFPL) exists, subclass it.



src/bots/vrlg/
├─ __init__.py
├─ strategy.py # Orchestrates tasks; lifecycle; exposes CLI
├─ data_feed.py # Async WS subscriptions; 100ms feature clock
├─ rotation_detector.py # Autocorr-based period R* + p-value gating
├─ signal_detector.py # 4-condition gate (phase/DoB/spread/OBI)
├─ execution_engine.py # Post-only Iceberg, TTL, OCO/IOC, cooldown
├─ risk_management.py # Kill-switches, exposure/slippage/book-impact
├─ config.py # Pydantic models; load from YAML/TOML via hl_core
└─ metrics.py # Prometheus exporter (counters/gauges/histograms)

backtest/
└─ vrlg_sim.py # 100ms queue simulator + latency model

monitoring/
└─ vrlg.json # Grafana dashboard (metrics panels)

scripts/
└─ record_ws.py # Live recorder: blocks/L2/trades -> parquet

configs/
└─ strategy.toml # (VRLG) parameters; see schema below


**Reuse** from PFPL/`hl_core`: logger, config loader/validator, backtest harness, CI settings, API client skeletons, Prometheus exporter patterns. If a function already exists in `hl_core`, call it; otherwise, implement the smallest reusable primitive in `hl_core` and use it from both PFPL and VRLG.

---

## 2) Data & Preprocessing

### Feeds (target refresh; push assumed)
- **Block meta**: `blocks` WS, ≤100 ms. Fields: `height`, `timestamp` (used for **period detection**).
- **Orderbook L2**: `level2` WS, ≤50 ms. Fields: `bestBid`, `bestAsk`, `bidSize_L1`, `askSize_L1`.
- **Trades (optional)**: `trades` WS, ≤50 ms. Used only to validate “thin moments” and execution quality.

### Derived features (update every 100 ms slab)
- `mid_t = (bestBid + bestAsk) / 2`
- `spread_ticks_t = (bestAsk - bestBid) / tick_size`
- `DoB_t = bidSize_L1 + askSize_L1`  (Top‑of‑Book depth)
- `OBI_t = (bidSize_L1 - askSize_L1) / max(bidSize_L1 + askSize_L1, 1e-9)` (imbalance)
- `block_phase_t ∈ [0,1]` (from rotation detector)

### Latency budget (targets, not hard guarantees)
- **WS → feature update**: ≤ **5 ms**
- **Signal → order submit** (in‑process queue): ≤ **200 μs**

---

## 3) Rotation‑Equivalent Timing Detection (no fixed period)

On a rolling window (**T_roll = 30 s**), compute **autocorrelation** over `{DoB_t, spread_ticks_t}` to estimate the strongest repeating period **R\*** (seconds).  
Define `block_phase_t = (t mod R*) / R*`. Validate that near phase boundaries (0/1) the pattern holds (thin DoB, wider spread).

**Gating with significance**: require the effect to reproduce over ≥ **200** boundary samples with **p‑value ≤ 0.01**.  
If p‑value degrades above the threshold, **PAUSE** the strategy (“observe mode”) until it recovers.

---

## 4) Formal Signal (parameters & predicates)

Parameters (defaults; must be editable via config):
- `N = 80` (for rolling median)
- `x = 0.25` (DoB threshold = below **75%** of median)
- `y = 2` (spread threshold = **≥ 2 ticks**)
- `z = 0.6` (phase window width = ±`0.6/M` period; implement as boundary band)

**ON if ALL hold simultaneously**:
1. `phase_gate`: `block_phase_t ∈ {0±z} ∪ {1±z}`
2. `DoB_t < median(DoB_{t−N:t}) × (1 − x)`
3. `spread_ticks_t ≥ y`
4. `|OBI_t| ≤ 0.6` (avoid one‑sided sweeps; stay near fair value)

Emit `Signal(now, mid)`.

---

## 5) Entry/Exit/Stops (execution model)

- **Order type**: **postOnly + Iceberg**
- **Prices**: `mid_t ± 0.5 tick` (two‑sided; allow single‑sided bias switch in config)
- **Display size**: `display = min(max(total×0.25, 0.01 BTC), total)`
- **TTL**: **1.0 s** (cancel if not filled)
- **Size**: **0.2–0.5%** of account equity (allow split into micro‑clips)

**Exit**:
- Ideal: within **≤ 500 ms** post fill, flatten via **market IOC**.
- Fallback: on `spread_ticks_t` collapsing to `1`, commit the opposite side with IOC.

**Stops (OCO)**:
- Protective stop: `mid_t ± 3 ticks` (reverse stop)
- **Time‑Stop**: **1.2 s** → force close, regardless.

**Cooldown**:
- After a **fill** on one side, **no same‑direction entries for `2×R*` seconds**.

---

## 6) Rule‑Based Risk Management

| Monitor                       | Threshold                   | Action                                                     |
|------------------------------|-----------------------------|------------------------------------------------------------|
| Block interval (rolling med) | `> 4 s`                     | **Flat immediately** & stop strategy                      |
| Book impact (own orders/Top) | `> 2% / 5 s`                | **Halve size**                                            |
| Slippage (avg fill − mid)    | `> 1 tick (1 min avg)`      | Go deeper with postOnly; **forbid market entries**        |
| Consecutive stopouts         | `≥ 3 in 10 min`             | **Pause 10 min**                                          |
| Cross‑bucket correlation     | `NetΔ > 0.8 × VaR_1s`       | **Issue hedge** order (configurable)                      |

Implement **kill‑switch** hooks; when triggered: flatten, cancel, notify.

---

## 7) Implementation Plan (what to code)

### `data_feed.py`
- Async WS clients (`blocks`, `level2`, `trades`) using `hl_core.api.ws`.
- 100 ms feature clock aggregating to `mid`, `spread_ticks`, `DoB`, `OBI`.
- Publish feature snapshots into an `asyncio.Queue`.

~~~python
# data_feed.py — what each function must do
async def run_feeds(cfg, q_features) -> None:
    """Connect to WS (blocks, level2, trades), maintain rolling state,
    compute derived features every 100 ms, and put FeatureSnapshot into q_features."""

def compute_features(book) -> FeatureSnapshot:
    """Given best bid/ask and sizes, compute mid, spread in ticks, DoB, OBI."""
~~~

### `rotation_detector.py`
- Rolling buffers (≥30 s), **autocorr** over DoB/spread → estimate `R*`.
- Validate with a **phase‑aligned** test; compute **p‑value**; expose `phase(t)`.

~~~python
# rotation_detector.py
class RotationDetector:
    """Maintain rolling series; estimate R* via autocorrelation; compute block_phase_t;
    test significance; expose is_active flag."""

    def update(self, t: float, dob: float, spread_ticks: float) -> None: ...
    def current_phase(self, t: float) -> float: ...
    def is_active(self) -> bool: ...
    def current_period(self) -> float | None: ...
~~~

### `signal_detector.py`
- 4‑gate logic (phase/DoB/spread/OBI) with rolling **median** over `N`.

~~~python
# signal_detector.py
class SignalDetector:
    """Evaluate VRLG signal predicates; emit Signal when all gates pass."""

    def update_and_maybe_signal(self, t: float, features: FeatureSnapshot) -> Signal | None: ...
~~~

### `execution_engine.py`
- **Post‑only Iceberg** maker orders at `mid ± 0.5 tick`, **TTL = 1.0 s**.
- **IOC** unwind, **OCO** protective stops, **cooldown** by `2×R*`.
- Uses `hl_core.api.http` adapters (placeholders if not available).

~~~python
# execution_engine.py
class ExecutionEngine:
    """Submit/cancel orders; manage TTL; OCO stops; IOC exits; enforce cooldown."""

    async def place_two_sided(self, mid: float, sz: float) -> list[str]: ...
    async def wait_fill_or_ttl(self, order_ids: list[str], timeout_s: float) -> None: ...
    async def flatten_ioc(self) -> None: ...
~~~

### `risk_management.py`
- Enforce **tabled rules**; surface **kill‑switch** method.

~~~python
# risk_management.py
class RiskManager:
    """Track slippage, book impact, consecutive stopouts, block intervals; fire kill-switches."""

    def should_pause(self) -> bool: ...
    def register_fill(self, fill) -> None: ...
~~~

### `strategy.py`
- Orchestrate tasks: `data_feed` → `rotation_detector` → `signal_detector` → `execution_engine` + `risk_manager`.
- Provide CLI (`--paper`, `--live`, `--config`), graceful shutdown, Prometheus endpoint.

~~~python
# strategy.py
class VRLGStrategy(BaseStrategy | object):
    """Wire data pipeline and execution; load config; export metrics; run main loop."""

async def main() -> None:
    """Entry point (uvloop), parse args, run VRLGStrategy."""
~~~

### `config.py`
- Pydantic models; load TOML/YAML via `hl_core.utils.config`. Expose schema identical to the sample below.

### `metrics.py`
- Export Prometheus metrics: `vrlg_signal_count`, `fill_rate`, `slippage_ticks`,
  `block_interval_ms`, `orders_rejected` (+ histograms for holding time & PnL/trade).

---

## 8) Backtest

- **Recorder** (`scripts/record_ws.py`): save `blocks`/`L2` (and optional `trades`) to **parquet**.
- **Simulator** (`backtest/vrlg_sim.py`): 100 ms queue, **price‑time priority**, partial fills, cancels.
- **Latency injection**:  
  - Ingest: **10–30 ms**; Order/Cxl RTT: **30–80 ms**.
- **Metrics**: `HitRate`, `Avg PnL/trade (bps)`, `Slip (ticks)`, `Holding (ms)`, `Trades/min`, `MaxDD`.
- **Parameter sweep** (`optuna`):  
  - `x ∈ {0.15, 0.2, 0.25, 0.3}`, `y ∈ {1, 2, 3}`, `TTL ∈ {0.6, 1.0, 1.4}s`.  
  - **Objective**: `Sharpe – 0.5 × MaxDD – 0.1 × mean|Δ|`.

---

## 9) Config (TOML schema)

~~~toml
[symbol]
name      = "BTCUSD-PERP"
tick_size = 0.5

[signal]
N = 80    # median window
x = 0.25  # DoB threshold
y = 2     # spread threshold (ticks)
z = 0.6   # phase width (relative to period)

[exec]
order_ttl_ms     = 1000
display_ratio    = 0.25
min_display_btc  = 0.01
max_exposure_btc = 0.8
cooldown_factor  = 2.0  # 2×R* seconds

[risk]
max_slippage_ticks = 1
max_book_impact    = 0.02   # 2% of TopDepth per 5s
time_stop_ms       = 1200
stop_ticks         = 3

[latency]
ingest_ms   = 10
order_rt_ms = 60
~~~

---

## 10) CLI & Monitoring

- CLI options: `--config`, `--paper/--live`, `--prom-port`, `--log-level`.
- Expose `/metrics` for Prometheus; ship **Grafana** dashboard `monitoring/vrlg.json` with:
  - signal/fill/slippage time‑series
  - spread/DoB **heatmap**
  - block interval histogram

---

## 11) Runbook (operational rules)

- **Pre‑start (daily)**: WS RTT `< 80 ms`; block interval p50 `< 2.5 s`; capital allocation OK.
- **During run**: if any kill‑switch fires → **auto flat**, cancel, notify (Slack/Discord hook).
- **Shutdown**: rotate logs, write parquet, update next‑day `R*` estimate.

---

## 12) Definition of Done (acceptance gates)

- **Paper 48 h**: `Slip_avg ≤ 1 tick`, `WinRate ≥ 55%`, `MaxDD ≤ 8× daily edge`, `Trades/min ∈ [3,10]`.  
- **Live min size**: `0.001 BTC` after paper KPI pass; then scale under risk caps.

---

## 13) Quality Bar & Coding Rules

- Fully **typed**, docstring each public function (“what it does” first line).  
- **No busy‑wait**; clean `asyncio` tasks and cancellation.  
- **One logger** from `hl_core.utils.logger`.  
- **Config** only via `hl_core.utils.config`.  
- **Unit tests** for detectors; **integration tests** for strategy path; **e2e** gated by live keys.  
- Keep **PFPL** and **VRLG** common parts inside `hl_core` (don’t duplicate).

---

## 14) Example Flow (pseudocode sketch)

~~~python
# main loop sketch
while running:
    feat = await q_features.get()
    rot.update(feat.t, feat.dob, feat.spread_ticks)
    if not rot.is_active():
        continue
    phase = rot.current_phase(feat.t)
    sig = sigdet.update_and_maybe_signal(feat.t, feat._replace(block_phase=phase))
    if not sig or risk.should_pause():
        continue
    order_ids = await exe.place_two_sided(sig.mid, size_allocator.next())
    await exe.wait_fill_or_ttl(order_ids, timeout_s=cfg.exec.order_ttl_ms/1000)
    await exe.flatten_ioc()
~~~

**End of prompt. Start implementing now.**
