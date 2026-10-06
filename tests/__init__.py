import os
import sys
from pathlib import Path

# Let `python -m unittest` find the package without installing it.
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Unit tests never check PyPI or upgrade the install running them.
os.environ.setdefault("CLAUDE_ARTIFACT_NO_UPDATE_CHECK", "1")
