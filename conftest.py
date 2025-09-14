"""Pytest configuration for project root.

Ensures source package is importable and uses certifi for SSL
verification when available.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import certifi

# Add the ``src`` directory to ``sys.path`` so that ``hl_core`` and other
# packages are importable without installation.
SRC_PATH = Path(__file__).resolve().parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

# Use certifi's CA bundle for libraries that consult these environment
# variables (e.g. requests, httpx, websockets).
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

logging.basicConfig(level=logging.DEBUG)
