import sys
from pathlib import Path

# Let `python -m unittest` find the package without installing it.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
