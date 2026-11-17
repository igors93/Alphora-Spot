"""Source-checkout shim for ``python main.py``; the engine lives in ``src/engine.py``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from engine import run  # noqa: E402

if __name__ == "__main__":
    run()
