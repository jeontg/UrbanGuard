"""Latest risk state + short history per block (thread-safe).

Ported verbatim from flood3's ``service/store.py``. The background pipeline
runner (Phase 7 below) calls ``update()``; FastAPI handlers read it. In-memory
for this PoC; a production deployment would swap this for TimescaleDB or
similar (flood3's own IMPLEMENTATION_PLAN.md already noted this).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RiskStore:
    def __init__(self, history: int = 180):
        self._lock = threading.Lock()
        self._cur: dict[str, dict] = {}
        self._hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=history))
        self.started_at = time.time()

    def update(self, block_id: str, snap: dict) -> None:
        with self._lock:
            self._cur[block_id] = snap
            self._hist[block_id].append({
                "t_sec": snap["t_sec"], "severity": snap["severity"],
                "speed_drop": snap["speed_drop"], "rain_mm_h": snap["rain_mm_h"],
                # flood risk (measurement/prediction) — None if water_available=False
                "flood_risk_score": snap.get("flood_risk_score"),
                "water_area_ratio": snap.get("water_area_ratio")})

    def all(self) -> list[dict]:
        # drop the base64 snapshot image to keep polling lightweight
        with self._lock:
            return [{k: v for k, v in s.items() if k != "snapshot"}
                    for s in self._cur.values()]

    def get(self, block_id: str) -> dict | None:
        with self._lock:
            s = self._cur.get(block_id)
            if not s:
                return None
            return {**s, "history": list(self._hist[block_id])}

    def all_history(self) -> dict:
        """Per-block risk-level history (for the timeline chart). Preserves
        block order."""
        with self._lock:
            return {bid: {"name": self._cur.get(bid, {}).get("name", bid),
                          "points": list(h)} for bid, h in self._hist.items()}
