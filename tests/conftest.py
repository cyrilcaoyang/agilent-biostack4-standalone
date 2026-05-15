"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python -m pytest` to import the package without an install step.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
