"""River-level providers (environmental sensor stage) — 침수(flood) 소유.

Ported from flood3's ``perception/river_provider.py`` via
``traffic_weather/perception/river_provider.py``.

★ 2026-08-21 flood/traffic 도메인 분리: 하천 수위는 명백한 **직접 침수
신호**인데, 예전에는 ``traffic_weather``(교통·기상 패키지) 아래에 있었다.
이 파일을 flood 소유로 옮기고, 옛 위치(``traffic_weather/perception/
river_provider.py``)는 이 모듈을 그대로 재-export하는 얇은 호환 shim으로
남겼다.

- ``NoRiverProvider``   — blocks with no adjacent river (always None).
- ``MockRiverProvider`` — periodic demo water level.
- ``HrfcoRiverProvider``— Korea's flood control office (HRFCO) OpenAPI level
  (10-min). Missing key / fetch failure falls back.

``build_river(cfg)`` selects a provider per blocks.json. Threshold levels
(advisory/warning/danger, m) come from config.
"""
from __future__ import annotations

import json
import math
import os
import ssl
import urllib.request
from typing import Protocol

from ..models import RiverState, RiverStatus


class RiverProvider(Protocol):
    def at(self, t_sec: float) -> RiverState | None: ...


def _status(level: float, thr: dict) -> tuple[RiverStatus, float]:
    danger = thr.get("danger_m", 4.0)
    ratio = level / danger if danger > 0 else 0.0
    if level >= danger:
        return RiverStatus.danger, ratio
    if level >= thr.get("warning_m", 3.2):
        return RiverStatus.alert, ratio
    if level >= thr.get("advisory_m", 2.5):
        return RiverStatus.advisory, ratio
    return RiverStatus.normal, ratio


class NoRiverProvider:
    def at(self, t_sec: float) -> RiverState | None:
        return None


class MockRiverProvider:
    """Periodic water level (continuous demo). Phase offsets stagger blocks."""

    def __init__(self, thr: dict, base_m: float = 1.5, peak_m: float = 4.2,
                 period_sec: float = 60.0, phase_sec: float = 0.0,
                 station: str = "mock-station"):
        self.thr = thr
        self.base = base_m
        self.peak = peak_m
        self.period = period_sec
        self.phase = phase_sec
        self.station = station

    def at(self, t_sec: float) -> RiverState:
        s = 0.5 * (1 - math.cos(2 * math.pi * (t_sec + self.phase) / self.period))
        level = round(self.base + (self.peak - self.base) * s, 2)
        status, ratio = _status(level, self.thr)
        return RiverState(level_m=level, status=status, ratio=round(ratio, 3),
                          station=self.station, source="mock")


class HrfcoRiverProvider:
    """HRFCO water-level OpenAPI (10-min). TTL cache + fallback."""

    def __init__(self, obs_code: str, service_key: str, thr: dict,
                 ttl_sec: float = 300.0, fallback: RiverProvider | None = None):
        self.obs_code = obs_code
        self.key = service_key
        self.thr = thr
        self.ttl = ttl_sec
        self.fallback = fallback
        self._cached: float | None = None
        self._fetched_at = 0.0

    def _fetch(self) -> float:
        url = f"http://api.hrfco.go.kr/{self.key}/waterlevel/list/10M/{self.obs_code}.json"
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=8, context=ctx) as r:
            j = json.loads(r.read().decode("utf-8"))
        rows = j.get("content", []) or []
        for row in reversed(rows):  # most recent first
            wl = str(row.get("wl", "")).strip()
            if wl not in ("", "None"):
                return float(wl)
        return 0.0

    def at(self, t_sec: float) -> RiverState | None:
        import time
        now = time.time()
        if self._cached is None or (now - self._fetched_at) >= self.ttl:
            try:
                self._cached = self._fetch()
                self._fetched_at = now
            except Exception as e:  # noqa: BLE001
                if self._cached is None:
                    return self.fallback.at(t_sec) if self.fallback else None
                print(f"[river/hrfco] fetch failed (using cache): {str(e)[:100]}")
        level = self._cached or 0.0
        status, ratio = _status(level, self.thr)
        return RiverState(level_m=round(level, 2), status=status, ratio=round(ratio, 3),
                          station=self.obs_code, source="hrfco")


def build_river(cfg: dict | None) -> RiverProvider:
    """blocks.json ``river`` config -> provider. type: none(default)|mock|hrfco."""
    cfg = cfg or {"type": "none"}
    t = cfg.get("type", "none")
    thr = {k: cfg[k] for k in ("advisory_m", "warning_m", "danger_m") if k in cfg}
    if t == "mock":
        return MockRiverProvider(thr, base_m=cfg.get("base_m", 1.5),
                                 peak_m=cfg.get("peak_m", 4.2),
                                 period_sec=cfg.get("period", 60.0),
                                 phase_sec=cfg.get("phase", 0.0),
                                 station=cfg.get("station", "mock-station"))
    if t == "hrfco":
        key = os.environ.get("HRFCO_API_KEY")
        mock = MockRiverProvider(thr, peak_m=cfg.get("peak_m", 4.2),
                                 period_sec=cfg.get("period", 60.0),
                                 phase_sec=cfg.get("phase", 0.0))
        if not key:
            print("[river] HRFCO_API_KEY not set -> mock river fallback")
            return mock
        return HrfcoRiverProvider(cfg.get("obs_code", ""), key, thr, fallback=mock)
    return NoRiverProvider()
