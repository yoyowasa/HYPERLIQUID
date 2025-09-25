from __future__ import annotations

from typing import Any

# 〔この import がすること〕 src レイアウト/実行環境差に備えた二段構えの import
try:
    from bots.vrlg.signal_detector import SignalDetector
    from bots.vrlg.data_feed import FeatureSnapshot
except Exception:
    from src.bots.vrlg.signal_detector import SignalDetector  # type: ignore
    from src.bots.vrlg.data_feed import FeatureSnapshot  # type: ignore


def _cfg(N=10, x=0.25, y=2.0, z=0.2, obi_limit=0.6) -> dict[str, Any]:
    """〔この関数がすること〕 シグナル設定を最小化した辞書を返します。"""
    return {
        "signal": {"N": N, "x": x, "y": y, "z": z, "obi_limit": obi_limit},
        "symbol": {"tick_size": 0.5, "name": "BTCUSD-PERP"},
    }


def _feat(t, mid, spread, dob, obi, phase=None) -> FeatureSnapshot:
    """〔この関数がすること〕 手軽に FeatureSnapshot を生成します。"""
    return FeatureSnapshot(t=t, mid=mid, spread_ticks=spread, dob=dob, obi=obi, block_phase=phase)


def test_signal_detector_triggers() -> None:
    """〔このテストがすること〕 位相オン+薄板+広スプ+OBI内で Signal を返すことを確認します。"""
    cfg = _cfg(N=8, y=2.0, z=0.2)  # 中央値窓は小さめ
    det = SignalDetector(cfg)

    # DoB ヒストリー（中央値 ≈ 1000）を作る
    for i in range(8):
        det.update_and_maybe_signal(i * 0.1, _feat(i * 0.1, 100.0, 1.0, 1000.0, 0.0, phase=0.5))

    # 発火条件: phase∈{0±0.2}, dob < 1000*(1-0.25)=750, spread>=2, |obi|<=0.6
    sig = det.update_and_maybe_signal(1.0, _feat(1.0, 100.0, 2.0, 600.0, 0.1, phase=0.05))
    assert sig is not None, "条件成立でシグナルが発火しませんでした"


def test_signal_detector_phase_off_no_signal() -> None:
    """〔このテストがすること〕 位相オフのときは発火しないことを確認します。"""
    cfg = _cfg(N=8, y=2.0, z=0.2)
    det = SignalDetector(cfg)
    for i in range(8):
        det.update_and_maybe_signal(i * 0.1, _feat(i * 0.1, 100.0, 1.0, 1000.0, 0.0, phase=0.5))
    sig = det.update_and_maybe_signal(1.0, _feat(1.0, 100.0, 3.0, 600.0, 0.1, phase=0.4))
    assert sig is None, "位相オフでもシグナルが出てしまいました"


def test_signal_detector_spread_too_small() -> None:
    """〔このテストがすること〕 スプレッドが閾値未満なら発火しないことを確認します。"""
    cfg = _cfg(N=8, y=3.0, z=0.2)
    det = SignalDetector(cfg)
    for i in range(8):
        det.update_and_maybe_signal(i * 0.1, _feat(i * 0.1, 100.0, 1.0, 1000.0, 0.0, phase=0.5))
    sig = det.update_and_maybe_signal(1.0, _feat(1.0, 100.0, 2.0, 600.0, 0.1, phase=0.01))
    assert sig is None, "スプレッド不足でもシグナルが出てしまいました"


def test_signal_detector_obi_limit() -> None:
    """〔このテストがすること〕 OBI が上限を超えると発火しないことを確認します。"""
    cfg = _cfg(N=8, y=2.0, z=0.2, obi_limit=0.3)
    det = SignalDetector(cfg)
    for i in range(8):
        det.update_and_maybe_signal(i * 0.1, _feat(i * 0.1, 100.0, 1.0, 1000.0, 0.0, phase=0.5))
    sig = det.update_and_maybe_signal(1.0, _feat(1.0, 100.0, 3.0, 600.0, 0.5, phase=0.01))
    assert sig is None, "OBI上限超過でもシグナルが出てしまいました"
