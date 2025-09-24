# 〔このモジュールがすること〕
# VRLG 戦略の司令塔。設定ロード、タスク起動/停止、シグナル処理の骨組みを提供します。
# 実際のデータ購読・シグナル検出・執行・リスク管理は後続ステップで実装して差し込みます。

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Any, Optional

# uvloop があれば高速化（なくても動く）
try:
    import uvloop  # type: ignore
except Exception:  # pragma: no cover
    uvloop = None  # type: ignore

# 〔この関数がすること〕: 共通ロガー/コンフィグ読込は PFPL と共有（hl_core）を使います。
from hl_core.utils.logger import get_logger
from hl_core.utils.config import load_config

# 〔この import 群がすること〕
# データ購読・位相検出・シグナル判定・発注・リスク管理の各コンポーネントを司令塔に読ませます。
from .data_feed import run_feeds, FeatureSnapshot
from .rotation_detector import RotationDetector
from .signal_detector import SignalDetector
from .execution_engine import ExecutionEngine
from .risk_management import RiskManager

logger = get_logger("VRLG")


class VRLGStrategy:
    """〔このクラスがすること〕
    戦略全体のライフサイクルを管理します:
    - 設定を読み込む
    - キューや各コンポーネント（後で実装）を初期化する
    - 非同期タスク（データ→シグナル→執行）を起動/停止する
    - 将来、Prometheus のメトリクス公開を行う
    """

    def __init__(self, config_path: str, paper: bool) -> None:
        """〔このメソッドがすること〕
        TOML/YAML 設定を読み込み、実行モード（paper/live）を保持します。
        """
        self.config_path = config_path
        self.paper = paper
        self.cfg: Any = load_config(config_path)
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

        # 〔この属性がすること〕: 各段の非同期パイプ
        self.q_features: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self.q_signals: asyncio.Queue = asyncio.Queue(maxsize=1024)

        # 〔この属性がすること〕直近の特徴量を保持し、発注時に板消費率などの参照に使います。
        self._last_features: Optional[FeatureSnapshot] = None

        # 〔この属性がすること〕: 各コンポーネントの実体を生成し司令塔に保持します。
        self.rot = RotationDetector(self.cfg)
        self.sigdet = SignalDetector(self.cfg)
        self.exe = ExecutionEngine(self.cfg, paper=self.paper)
        self.risk = RiskManager(self.cfg)

    async def start(self) -> None:
        """〔このメソッドがすること〕
        戦略の主要タスクを起動します。
        - data_feed: WS購読→100ms特徴量を q_features へ
        - _signal_loop: 特徴量→位相推定→シグナル判定→q_signals へ
        - _exec_loop: シグナル消費→発注/キャンセル/クローズ
        """
        logger.info("VRLG starting (paper=%s, cfg=%s)", self.paper, self.config_path)

        # 〔この行がすること〕WebSocket購読→100ms特徴量生成タスクを起動します。
        self._tasks.append(asyncio.create_task(run_feeds(self.cfg, self.q_features), name="data_feed"))

        self._tasks.append(asyncio.create_task(self._signal_loop(), name="signal_loop"))
        self._tasks.append(asyncio.create_task(self._exec_loop(), name="exec_loop"))

    async def _signal_loop(self) -> None:
        """〔このメソッドがすること〕
        特徴量を受け取り、位相推定と 4 条件ゲートのシグナル判定を行い、
        条件合致時は q_signals に投入します。
        """
        while not self._stopping.is_set():
            try:
                feat = await self.q_features.get()
                self.rot.update(feat.t, feat.dob, feat.spread_ticks)
                self._last_features = feat
                if not self.rot.is_active():
                    continue
                phase = self.rot.current_phase(feat.t)
                self.exe.set_period_hint(self.rot.current_period() or 1.0)
                sig = self.sigdet.update_and_maybe_signal(feat.t, feat.with_phase(phase))
                if sig:
                    await self.q_signals.put(sig)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover
                logger.exception("signal_loop error: %s", e)

    async def _exec_loop(self) -> None:
        """〔このメソッドがすること〕
        シグナルを受けてリスク判定→post-only Iceberg の発注→TTL待ち→IOC解消を実行します。
        """
        while not self._stopping.is_set():
            try:
                sig = await self.q_signals.get()

                # リスク助言（キル/一時停止/サイズ倍率/成行禁止など）
                adv = self.risk.advice()
                if self.risk.should_pause() or adv.killswitch:
                    if adv.killswitch:
                        await self.exe.flatten_ioc()  # 念のため即フラット
                    continue

                # クリップサイズ: 最大エクスポージャの5%を基準にリスク倍率を反映
                max_expo = self.exe.max_exposure
                clip = max_expo * 0.05
                clip *= adv.size_multiplier
                clip = max(0.0, min(clip, max_expo))

                # 板消費率トラッキングのため display を事前計算（Feature の DoB 使用）
                if self._last_features is not None:
                    display = min(clip, max(clip * self.exe.display_ratio, self.exe.min_display))
                    self.risk.register_order_post(display_size=display, top_depth=self._last_features.dob)

                order_ids = await self.exe.place_two_sided(sig.mid, clip)
                await self.exe.wait_fill_or_ttl(order_ids, timeout_s=self.cfg.exec.order_ttl_ms / 1000)

                # 成行禁止でなければ IOC で素早く解消
                if not adv.forbid_market:
                    await self.exe.flatten_ioc()
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover
                logger.exception("exec_loop error: %s", e)

    async def shutdown(self) -> None:
        """〔このメソッドがすること〕
        全タスクを安全に停止し、フラット化（必要なら）して終了します。
        """
        if self._stopping.is_set():
            return
        logger.info("VRLG shutting down…")
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.exe:
            await self.exe.flatten_ioc()
        logger.info("VRLG stopped.")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """〔この関数がすること〕
    CLI 引数を解釈します。（--config, --paper/--live, --log-level）
    """
    p = argparse.ArgumentParser(prog="vrlg", description="VRLG high-frequency bot")
    p.add_argument("--config", required=True, help="path to VRLG config (TOML/YAML)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true", help="paper trading mode")
    g.add_argument("--live", action="store_true", help="live trading mode")
    p.add_argument("--log-level", default="INFO", help="logging level")
    return p.parse_args(argv)


async def _run(argv: list[str]) -> int:
    """〔この関数がすること〕
    uvloop を可能なら有効化し、VRLGStrategy を起動してシグナルで停止します。
    """
    args = parse_args(argv)
    if uvloop is not None:
        uvloop.install()
    strategy = VRLGStrategy(config_path=args.config, paper=not args.live)
    await strategy.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _handle_sig(*_: object) -> None:
        stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _handle_sig)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    await stop.wait()
    await strategy.shutdown()
    return 0


def main() -> None:
    """〔この関数がすること〕
    エントリポイント。例外を整形して終了コードを返します。
    """
    try:
        exit_code = asyncio.run(_run(sys.argv[1:]))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
