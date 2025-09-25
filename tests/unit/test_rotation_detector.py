from __future__ import annotations

from types import SimpleNamespace

# 実行環境の import パス差に対応
try:
    from bots.vrlg.rotation_detector import RotationDetector
except Exception:
    from src.bots.vrlg.rotation_detector import RotationDetector  # type: ignore


def _cfg(T_roll=120.0, z=0.15) -> SimpleNamespace:
    """〔この関数がすること〕 RotationDetector 用のミニマム設定を返します。"""
    return SimpleNamespace(signal=SimpleNamespace(T_roll=T_roll, z=z))


def test_rotation_detector_active() -> None:
    """〔このテストがすること〕
    位相境界で DoB が薄く、Spread が広い周期（真の周期 2.0s）を与えると、
    detector.is_active() が True になり、推定周期が [1.6, 2.4] に入ることを確認します。
    """
    det = RotationDetector(_cfg(T_roll=120.0, z=0.15))
    dt = 0.1
    R_true = 2.0
    t = 0.0
    # 120s → 1200 ステップ（境界: 位相±0.15 → 約360サンプル >= 200 を満たす）
    for _ in range(int(120.0 / dt)):
        phase = (t % R_true) / R_true
        boundary = (phase < 0.15) or (phase > 0.85)
        dob = 600.0 if boundary else 1200.0     # 境界で薄い
        spr = 3.0 if boundary else 1.0          # 境界で広い
        det.update(t, dob, spr)
        t += dt

    assert det.is_active(), "周期ありデータで is_active() が True になりませんでした"
    p = det.current_period()
    assert p is not None and 1.6 <= p <= 2.4, f"推定周期が外れています: {p}"


def test_rotation_detector_inactive_without_structure() -> None:
    """〔このテストがすること〕
    DoB/Spread が一定（周期構造なし）の入力では is_active() が False であることを確認します。
    """
    det = RotationDetector(_cfg(T_roll=120.0, z=0.15))
    dt = 0.1
    t = 0.0
    for _ in range(int(120.0 / dt)):
        det.update(t, 1000.0, 1.0)  # 一定の DoB/Spread
        t += dt
    assert not det.is_active(), "周期構造なしで is_active() が True になってしまいました"
