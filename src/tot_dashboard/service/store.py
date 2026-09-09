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
        """스냅샷 1개를 반영한다.

        ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 이 클래스는 이제 침수
        전용(flood-service)·교통 전용(traffic-service) 두 프로세스가 각자
        독립된 인스턴스로 쓴다. 예전에는 한 프로세스가 침수·교통을 합친
        스냅샷 하나만 만들었기 때문에 `severity`/`speed_drop`(교통 전용
        필드)이 **항상** 있다고 가정하고 `snap[...]`로 직접 읽었다 — 침수
        전용 스냅샷에는 그 필드들이 아예 없어 여기서 `KeyError`로 죽었다
        (실기 확인). `.get()`으로 바꿔 어느 쪽 스냅샷이 와도 안전하게
        받는다 — 없는 값은 0/None으로 눕힌다(있는 셈 치지 않는다).
        """
        with self._lock:
            self._cur[block_id] = snap
            self._hist[block_id].append({
                "t_sec": snap.get("t_sec", 0.0), "severity": snap.get("severity", 0),
                "speed_drop": snap.get("speed_drop", 0.0),
                "rain_mm_h": snap.get("rain_mm_h", 0.0),
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

    def remove(self, block_id: str) -> bool:
        """이 지점의 현재 상태와 이력을 지운다.

        상시 탐지 대상에서 빠진 카메라(도메인 지정 해제·카메라 삭제)를
        정리할 때 쓴다 — 안 지우면 **더 이상 아무도 보지 않는 지점의 마지막
        스냅샷이 상황판에 그대로 남아**, 멈춘 값을 현재 상태로 읽게 된다
        (2026-08-22, PipelineRunner 동적 재구성과 함께 신설).

        지울 것이 없었으면 ``False`` 를 돌려준다 — 호출부가 "정말 있었나"를
        구분할 수 있어야 로그가 사실을 말한다.
        """
        with self._lock:
            had = self._cur.pop(block_id, None) is not None
            had = self._hist.pop(block_id, None) is not None or had
            return had

    def all_history(self) -> dict:
        """Per-block risk-level history (for the timeline chart). Preserves
        block order."""
        with self._lock:
            return {bid: {"name": self._cur.get(bid, {}).get("name", bid),
                          "points": list(h)} for bid, h in self._hist.items()}
