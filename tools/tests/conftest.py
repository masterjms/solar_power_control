"""저장소 루트를 sys.path 에 넣어 `tools.sim` 을 import 할 수 있게 한다."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
