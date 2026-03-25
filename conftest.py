"""Project-level pytest bootstrap.

Ensures the local ``src/`` tree is importable when tests are run directly from
the repository root without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
