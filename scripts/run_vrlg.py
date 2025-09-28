# scripts/run_vrlg.py
# 〔このスクリプトがすること〕
# VRLG Strategy を CLI から起動/終了します。uvloop を自動利用し、SIGINT/SIGTERM でグレースフル停止します。

from __future__ import annotations

import argparse
import asyncio
import signal

# 〔この import がすること〕 共通ロガーで一元管理されたロガーを使います
from hl_core.utils.logger import get_logger

logger = get_logger("VRLG.runner")

# 〔この import がすること〕 VRLG の戦略本体を直接起動します
try:
    from bots.vrlg.strategy import VRLGStrategy
except Exception:
    from src.bots.vrlg.strategy import VRLGStrategy  # type: ignore


def parse_args() -> argparse.Namespace:
    """〔この関数がすること〕 CLI 引数を解釈します。"""
    p = argparse.ArgumentParser(description="Run VRLG Strategy")
    p.add_argument("--config", required=True, help="path to strategy config (TOML/YAML)")
    p.add_argument("--live", action="store_true", help="run in live mode (default: paper)")
    p.add_argument("--prom-port", type=int, default=None, help="Prometheus metrics port (optional)")
    p.add_argument("--decisions-file", default=None, help="path to JSONL for decision logs (optional)")
    p.add_argument("--log-level", default="INFO", help="logger level: DEBUG/INFO/WARN/ERROR")
    return p.parse_args()


async def _main() -> int:
    """〔この関数がすること〕
    Strategy を起動し、停止シグナルを待ってから安全にシャットダウンします。
    """
    args = parse_args()

    # ログレベル反映（strategy 側でも反映するが、runner 側でも即時反映）
    try:
        logger.setLevel(str(args.log_level).upper())
    except Exception:
        pass

    # Strategy を作成・起動
    strat = VRLGStrategy(
        config_path=args.config,
        paper=not args.live,
        prom_port=args.prom_port,
        decisions_file=args.decisions_file,
    )
    await strat.start()

    # 停止イベントを待つ
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _set_stop(*_: object) -> None:
        """〔この関数がすること〕 OS シグナルを受けて停止フラグを立てます。"""
        stop.set()

    # シグナル登録（Windowsでは一部未対応）
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _set_stop)
        except NotImplementedError:
            pass

    # 待機 → 終了処理
    try:
        await stop.wait()
    finally:
        await strat.shutdown()

    return 0


def main() -> None:
    """〔この関数がすること〕 uvloop があれば利用し、非同期メインを実行します。"""
    try:
        import uvloop  # type: ignore
        uvloop.install()
    except Exception:
        pass

    try:
        code = asyncio.run(_main())
    except KeyboardInterrupt:
        code = 130
    except Exception as e:  # 予期せぬ例外でも exit code を返す
        logger.exception("runner failed: %s", e)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
