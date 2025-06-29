# src/hl_core/utils/logger.py
import logging
from pathlib import Path

def setup_logger(name: str = "common", log_root: str | Path = "logs") -> None:
    if logging.getLogger().handlers:
        return  # 既に初期化済み
    log_dir = Path(log_root) / name
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8"),
        ],
    )
