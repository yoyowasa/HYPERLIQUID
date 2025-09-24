# 〔このモジュールがすること〕
# VRLG 戦略の司令塔。設定ロード、タスク起動/停止、シグナル処理の骨組みを提供します。
# 実際のデータ購読・シグナル検出・執行・リスク管理は後続ステップで実装して差し込みます。

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from typing import Optional

# uvloop があれば高速化（なくても動く）
try:
    import uvloop  # type: ignore
except Exception:  # pragma: no cover
    uvloop = None  # type: ignore

# 〔この関数がすること〕: 共通ロガー/コンフィグ読込は PFPL と共有（hl_core）を使います。
from hl_core.utils.logger import get_logger
from hl_core.utils.config import load_config

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
        self.cfg = load_config(config_path)
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

        # 〔この属性がすること〕: 各段の非同期パイプ
        self.q_features: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self.q_signals: asyncio.Queue = asyncio.Queue(maxsize=1024)

        # 〔この属性がすること〕: 後続ステップで実体を注入するプレースホルダ
        self.rot = None  # RotationDetector
        self.sigdet = None  # SignalDetector
        self.exe = None  # ExecutionEngine
        self.risk = None  # RiskManager

    async def start(self) -> None:
        """〔このメソッドがすること〕
        戦略の主要タスクを起動します。
        - data_feed: WS購読→100ms特徴量を q_features へ
        - _signal_loop: 特徴量→位相推定→シグナル判定→q_signals へ
        - _exec_loop: シグナル消費→発注/キャンセル/クローズ
        """
        logger.info("VRLG starting (paper=%s, cfg=%s)", self.paper, self.config_path)

        # 後続ステップで本実装を差し込みます（ここでは骨組みのみ）
        # self.rot = RotationDetector(self.cfg)
        # self.sigdet = SignalDetector(self.cfg)
        # self.exe = ExecutionEngine(self.cfg, paper=self.paper)
        # self.risk = RiskManager(self.cfg)
        # self._tasks.append(asyncio.create_task(run_feeds(self.cfg, self.q_features), name="data_feed"))

        self._tasks.append(asyncio.create_task(self._signal_loop(), name="signal_loop"))
        self._tasks.append(asyncio.create_task(self._exec_loop(), name="exec_loop"))

    async def _signal_loop(self) -> None:
        """〔このメソッドがすること〕
        特徴量を受け取り、位相推定と 4 条件ゲートのシグナル判定を行います。
        条件合致時は q_signals に投入します（骨組み段階では未実装）。
        """
        while not self._stopping.is_set():
            try:
                feat = await self.q_features.get()
                # if not self.rot.is_active():
                #     continue
                # phase = self.rot.current_phase(feat.t)
                # sig = self.sigdet.update_and_maybe_signal(feat.t, feat.with_phase(phase))
                sig = None  # プレースホルダ
                if sig:
                    await self.q_signals.put(sig)
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover
                logger.exception("signal_loop error: %s", e)

    async def _exec_loop(self) -> None:
        """〔このメソッドがすること〕
        シグナルを受けて、post-only Iceberg の発注→TTL待ち→IOC解消を実行します。
        骨組み段階ではスリープのみです（後続で実装）。
        """
        while not self._stopping.is_set():
            try:
                sig = await self.q_signals.get()
                # order_ids = await self.exe.place_two_sided(sig.mid, sig.size)
                # await self.exe.wait_fill_or_ttl(order_ids, timeout_s=self.cfg.exec.order_ttl_ms/1000)
                # await self.exe.flatten_ioc()
                await asyncio.sleep(0)
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
        # if self.exe:
        #     await self.exe.flatten_ioc()
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
