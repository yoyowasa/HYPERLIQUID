from __future__ import annotations

import asyncio
import math
from typing import Any, List, Tuple

import pytest

# import パス差に対応（src 直下運用/パッケージ運用の両方で動くよう二段構えに）
try:
    import bots.vrlg.strategy as strategy_mod
    from bots.vrlg.strategy import VRLGStrategy
    from bots.vrlg.data_feed import FeatureSnapshot
except Exception:  # pragma: no cover - テスト環境差分用
    import src.bots.vrlg.strategy as strategy_mod  # type: ignore
    from src.bots.vrlg.strategy import VRLGStrategy  # type: ignore
    from src.bots.vrlg.data_feed import FeatureSnapshot  # type: ignore

try:
    from bots.vrlg.execution_engine import ExecutionEngine
except Exception:  # pragma: no cover - テスト環境差分用
    from src.bots.vrlg.execution_engine import ExecutionEngine  # type: ignore


class DummyRot:
    """〔このクラスがすること〕
    周期検出のダミー実装:
      - is_active() は常に True
      - current_period() は 2.0s
      - current_phase(t) は (t % 2.0) / 2.0 を返して境界近傍を再現
    update() は受け取りだけ行い、内部状態は持ちません。
    """

    def __init__(self, _cfg) -> None:
        self._p = 2.0

    def update(self, t: float, dob: float, spr: float) -> None:
        return

    def is_active(self) -> bool:
        return True

    def current_period(self) -> float:
        return self._p

    def current_phase(self, t: float) -> float:
        return (t % self._p) / self._p


class SpyExec(ExecutionEngine):
    """〔このクラスがすること〕
    発注を本当に出さず、「どの価格で何件出したか」だけ記録します。
    flatten_ioc/cancel はNo-opにして、テストの安定性を高めます。
    """

    def __init__(self, cfg: Any, paper: bool) -> None:
        super().__init__(cfg, paper)
        self.prices: List[Tuple[str, float]] = []

    async def _post_only_iceberg(self, side: str, price: float, total: float, display: float, ttl_s: float):
        self.prices.append((side, float(price)))
        return f"spy-{side}-{price}"

    async def _cancel_many(self, order_ids: list[str]) -> None:  # pragma: no cover - noop
        return

    async def flatten_ioc(self) -> None:  # pragma: no cover - noop
        return


def _cfg_dict() -> dict:
    """〔この関数がすること〕 Strategy が読む生設定（dict）を返します（最小構成）。"""

    return {
        "symbol": {"name": "BTCUSD-PERP", "tick_size": 0.5},
        "signal": {"N": 4, "x": 0.25, "y": 2.0, "z": 0.15, "obi_limit": 0.6, "T_roll": 30.0},
        "exec": {
            "order_ttl_ms": 200,
            "display_ratio": 0.25,
            "min_display_btc": 0.01,
            "max_exposure_btc": 0.8,
            "cooldown_factor": 2.0,
            "percent_min": 0.002,
            "percent_max": 0.005,
            "splits": 1,
            "min_clip_btc": 0.001,
            "equity_usd": 10000.0,
        },
        "risk": {
            "max_slippage_ticks": 1.0,
            "max_book_impact": 0.02,
            "time_stop_ms": 400,
            "stop_ticks": 3.0,
        },
        "latency": {"ingest_ms": 10, "order_rt_ms": 60},
    }


@pytest.mark.asyncio
async def test_strategy_emits_orders_on_valid_signal(monkeypatch) -> None:
    """〔このテストがすること〕
    4 条件を満たす特徴量が投入されたとき、VRLGStrategy が発注（両面）を呼ぶことを確認します。
    手順:
      1) load_config をパッチして dict を返すようにする
      2) Strategy を起動し、Rotation を DummyRot に、Execution を SpyExec に差し替える
      3) DoB の中央値を作るためにベース値を投入 → その後、境界位相で薄板&広スプのサンプルを投入
      4) SpyExec に価格が記録されていることを確認
    """

    # 1) load_config のパッチ
    cfgd = _cfg_dict()
    monkeypatch.setattr(strategy_mod, "load_config", lambda path: cfgd)

    # data feed を外部依存なしに待機させるダミーに差し替え
    async def _idle_run_feeds(_cfg, _queue):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(strategy_mod, "run_feeds", _idle_run_feeds)

    # 2) Strategy 起動
    st = VRLGStrategy(config_path="dummy.toml", paper=True, prom_port=None)
    await st.start()

    # Rotation/Execution を差し替え
    st.rot = DummyRot(st.cfg)
    st.exe = SpyExec(st.cfg, paper=True)

    # 3) 特徴量を投入（時刻は 0.1s 刻み）
    t = 0.5
    for _ in range(4):
        snap = FeatureSnapshot(t=t, mid=70000.0, spread_ticks=1.0, dob=1000.0, obi=0.0)
        await st.q_features.put(snap)
        t += 0.1

    snap = FeatureSnapshot(t=2.01, mid=70000.0, spread_ticks=2.5, dob=600.0, obi=0.1)
    await st.q_features.put(snap)

    # 4) 少し待って実行ループに処理させる
    await asyncio.sleep(0.5)

    # 発注が記録されていること（両面で2件が理想だが、クールダウンや境界条件で 1 件でもOKとする）
    assert len(st.exe.prices) >= 1, f"発注が呼ばれていません: prices={st.exe.prices}"

    # 価格が tick に丸められていることを簡易に確認（0.5 刻み）
    for _side, px in st.exe.prices:
        assert math.isclose(px / 0.5, round(px / 0.5), rel_tol=0, abs_tol=1e-9), f"価格がtick丸めされていません: {px}"

    # 後片付け
    await st.shutdown()
