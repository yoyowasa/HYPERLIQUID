# tests/unit/test_pfpl_init.py
"""
PFPLStrategy が import できて __init__ が走るかだけを確認する smoke-test
"""
from bots.pfpl import PFPLStrategy


def test_init() -> None:
    PFPLStrategy(config={})  # 例外が出なければ OK
