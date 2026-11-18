"""Test setup: isolate the database/config writes into a temp dir *before* backend imports."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="tablesense-tests-"))
os.environ.setdefault("TABLESENSE_DB", str(_TMP / "test.db"))
os.environ.setdefault("TABLESENSE_ZONES", str(_TMP / "zones.json"))

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
