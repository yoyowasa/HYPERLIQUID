from .strategy import run_live
from .strategy import PFPLStrategy

"""
PFPL (Points-Funding Phase-Lock) コントラリアン ― v2 基本骨格

非同期コンポーネント:
1. DataCollector      : API / WS 取得
2. FeatureStore       : 30 d リングバッファ
3. ScoringEngine      : ζ パーセンタイル & PnL σ
4. SignalGenerator    : エントリ / 決済判定
5. PositionManager    : レバ・サイズ計算とポジ管理
6. ExecutionGateway   : 発注 / キャンセル
7. RiskGuards         : エクスポージャ監視 & Kill-Switch
8. AnalysisLogger     : OPEN / CLOSE 行を CSV

共通ユーティリティは `hl_core` を使用。
"""
__all__: list[str] = ["run_live", "PFPLStrategy"]
