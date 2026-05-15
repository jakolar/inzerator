"""V2 location pipeline: address search → enqueue → worker spawns
gen_panorama.py + 3× gen_detail.py → exposes job state to UI."""
from __future__ import annotations
import threading
from pathlib import Path

TILES_DIR_PREFIX = "tiles_v2_"
JOB_LOG_DIR = Path("cache/jobs")
STEP_TIMEOUT_SECS = 60 * 60   # 60 min: cold ČÚZK DMR5G cache can take 10–30 min
STEP_NAMES = ("panorama", "outer", "closeup", "inner")

JOBS: dict[str, dict] = {}
JOB_QUEUE: list[str] = []
JOB_LOCK = threading.Lock()
JOB_CV = threading.Condition(JOB_LOCK)
CURRENT_JOB: str | None = None
