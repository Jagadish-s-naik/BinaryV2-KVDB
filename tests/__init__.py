import sys
from pathlib import Path

# Ensure src/ is on sys.path so 'import kvdb' works across tests
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
