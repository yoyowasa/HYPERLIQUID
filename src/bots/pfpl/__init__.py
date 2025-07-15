# src/bots/pfpl/__init__.py
from .strategy import PFPLStrategy  # re-export
import logging
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

__all__ = ["PFPLStrategy"]
1
# ─────────────────────────────────────────────────────────
#   PFPL package logger -> logs/pfpl/{pfpl.log,error.log}
# ─────────────────────────────────────────────────────────


def _ensure_pfpl_logger() -> None:
    logger = logging.getLogger("bots.pfpl")
    if any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers):
        return  # 既にセット済み

    log_dir = Path(__file__).resolve().parents[3] / "logs" / "pfpl"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ① ファイル出力ハンドラ
    fh_info = TimedRotatingFileHandler(
        log_dir / "pfpl.log", when="midnight", backupCount=14, encoding="utf-8"
    )
    fh_info.setLevel(logging.INFO)
    fh_err = TimedRotatingFileHandler(
        log_dir / "error.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fh_err.setLevel(logging.WARNING)

    # ② コンソール出力ハンドラ
    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh_info.setFormatter(fmt)
    fh_err.setFormatter(fmt)
    sh.setFormatter(fmt)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(fh_info)
    logger.addHandler(fh_err)
    logger.addHandler(sh)
    logger.propagate = False  # Runner には流さない


_ensure_pfpl_logger()
