"""Rainfall providers (environmental sensor stage).

Ported from flood3's ``perception/rainfall_provider.py``.
- ``MockRainfallProvider`` / ``SineRainfallProvider`` — deterministic demo rain.
- ``KmaRainfallProvider`` — Korea Meteorological Administration ultra-short-term
  nowcast (RN1, 1h rainfall mm). Converts block lat/lng to the KMA grid and
  TTL-caches the result; falls back on missing key / fetch failure so the
  pipeline never stalls.

``build_rainfall(cfg, coords, fallback, default_type)`` selects a provider per
blocks.json. ``cfg`` (camera-specific) always wins; ``default_type`` (the
global setting, ``core.settings.rainfall_backend()``) applies only when the
camera leaves ``rainfall`` unset — 2026-08-27.
"""
from __future__ import annotations

import json
import math
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Protocol

from ...models import WeatherState
from ..knowledge.ontology import rain_intensity


class RainfallProvider(Protocol):
    def at(self, t_sec: float) -> WeatherState: ...


class MockRainfallProvider:
    """Deterministic scenario: no rain -> heavy rain ramp -> hold."""

    def __init__(self, peak_mm_h: float = 25.0, ramp_start: float = 4.0,
                 ramp_end: float = 14.0):
        self.peak = peak_mm_h
        self.ramp_start = ramp_start
        self.ramp_end = ramp_end

    def at(self, t_sec: float) -> WeatherState:
        if t_sec <= self.ramp_start:
            mm = 0.0
        elif t_sec >= self.ramp_end:
            mm = self.peak
        else:
            frac = (t_sec - self.ramp_start) / (self.ramp_end - self.ramp_start)
            mm = round(self.peak * frac, 1)
        return WeatherState(t_sec=round(t_sec, 2), rain_mm_h=mm,
                            intensity=rain_intensity(mm), source="mock")


class SineRainfallProvider:
    """Periodic rain (continuous demo). Phase offsets stagger blocks."""

    def __init__(self, peak_mm_h: float = 25.0, period_sec: float = 48.0,
                 phase_sec: float = 0.0, floor_mm_h: float = 0.0):
        self.peak = peak_mm_h
        self.period = period_sec
        self.phase = phase_sec
        self.floor = floor_mm_h

    def at(self, t_sec: float) -> WeatherState:
        s = 0.5 * (1 - math.cos(2 * math.pi * (t_sec + self.phase) / self.period))
        mm = round(self.floor + (self.peak - self.floor) * s, 1)
        return WeatherState(t_sec=round(t_sec, 2), rain_mm_h=mm,
                            intensity=rain_intensity(mm), source="mock-sine")


# ──────────────────────────────────────────────────────────────────────────
# KMA grid conversion (LCC) — lat/lng -> (nx, ny)
# ──────────────────────────────────────────────────────────────────────────
def dfs_xy_conv(lat: float, lng: float) -> tuple[int, int]:
    RE, GRID = 6371.00877, 5.0
    SLAT1, SLAT2, OLON, OLAT, XO, YO = 30.0, 60.0, 126.0, 38.0, 43, 136
    d = math.pi / 180.0
    re = RE / GRID
    sl1, sl2, ol, oa = SLAT1 * d, SLAT2 * d, OLON * d, OLAT * d
    sn = math.tan(math.pi * 0.25 + sl2 * 0.5) / math.tan(math.pi * 0.25 + sl1 * 0.5)
    sn = math.log(math.cos(sl1) / math.cos(sl2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + sl1 * 0.5)
    sf = (sf ** sn) * math.cos(sl1) / sn
    ro = math.tan(math.pi * 0.25 + oa * 0.5)
    ro = re * sf / (ro ** sn)
    ra = math.tan(math.pi * 0.25 + lat * d * 0.5)
    ra = re * sf / (ra ** sn)
    theta = lng * d - ol
    if theta > math.pi:
        theta -= 2 * math.pi
    if theta < -math.pi:
        theta += 2 * math.pi
    theta *= sn
    nx = int(ra * math.sin(theta) + XO + 0.5)
    ny = int(ro - ra * math.cos(theta) + YO + 0.5)
    return nx, ny


class KmaRainfallProvider:
    """KMA ultra-short-term nowcast RN1 (1h rainfall mm). TTL cache + fallback."""

    URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"

    def __init__(self, lat: float, lng: float, service_key: str,
                 ttl_sec: float = 300.0, fallback: RainfallProvider | None = None):
        self.nx, self.ny = dfs_xy_conv(lat, lng)
        self.key = service_key
        self.ttl = ttl_sec
        self.fallback = fallback
        self._cached_mm: float | None = None
        self._fetched_at = 0.0

    @staticmethod
    def _base_dt(now: datetime | None = None) -> tuple[str, str]:
        t = (now or datetime.now()) - timedelta(minutes=45)
        return t.strftime("%Y%m%d"), t.strftime("%H") + "00"

    def _fetch(self) -> float:
        base_date, base_time = self._base_dt()
        q = urllib.parse.urlencode({
            "serviceKey": self.key, "dataType": "JSON", "numOfRows": 60, "pageNo": 1,
            "base_date": base_date, "base_time": base_time, "nx": self.nx, "ny": self.ny})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(self.URL + "?" + q, timeout=8, context=ctx) as r:
            j = json.loads(r.read().decode("utf-8"))
        items = j["response"]["body"]["items"]["item"]
        for it in items:
            if it.get("category") == "RN1":
                v = str(it.get("obsrValue", "0")).strip()
                try:
                    return max(0.0, float(v))
                except ValueError:
                    return 0.0
        return 0.0

    def at(self, t_sec: float) -> WeatherState:
        now = datetime.now().timestamp()
        if self._cached_mm is None or (now - self._fetched_at) >= self.ttl:
            # ⚠️ 실기 확인(2026-09-01) — `self._fetched_at = now`가 원래
            # try 블록 "성공 시"에만 있었다. KMA가 429(과다 요청)나 타임아웃을
            # 계속 돌려주면 `_fetched_at`이 갱신되지 않아 TTL 게이트가 매번
            # 참이 되고, 이 파이프라인 틱(초당 호출)마다 재시도 폭주로
            # 이어진다 — 실제로 platform-shell 로그에 이 실패 메시지가
            # 12만 건 이상 연속으로 찍힌 것을 이번 점검 중 발견했다(당시
            # KMA 백엔드가 전역 기본값이었던 시기, 지금은 dormant). 성공/실패
            # 관계없이 시도 시각을 먼저 찍어 TTL 동안은 반드시 쉬게 한다.
            self._fetched_at = now
            try:
                self._cached_mm = self._fetch()
            except Exception as e:  # noqa: BLE001
                if self._cached_mm is None:
                    if self.fallback is not None:
                        return self.fallback.at(t_sec)
                    self._cached_mm = 0.0
                print(f"[rainfall/kma] fetch failed (using cache/fallback): {str(e)[:100]}")
        mm = self._cached_mm or 0.0
        return WeatherState(t_sec=round(t_sec, 2), rain_mm_h=mm,
                            intensity=rain_intensity(mm), source="kma")


def build_rainfall(cfg: dict | None, coords: dict,
                   fallback: RainfallProvider,
                   default_type: str = "sine") -> RainfallProvider:
    """blocks.json ``rainfall`` config -> provider. type: sine(default)|mock|kma.

    ``cfg`` 는 **카메라별 명시 설정**이다 — 있으면 항상 이긴다. 카메라가
    아무것도 지정하지 않았을 때만 ``default_type``(전역 기본값, S-95
    ``core.settings.rainfall_backend()``)을 쓴다. 이렇게 나눠 둔 이유는
    39개소를 하나씩 고치지 않고도 관리자가 화면 설정 하나로 실측/합성을
    바꿀 수 있게 하려는 것이다(2026-08-27, 교통위험 실측 연동 요청).
    """
    cfg = cfg or {"type": default_type}
    t = cfg.get("type", default_type)
    if t == "kma":
        key = os.environ.get("KMA_SERVICE_KEY")
        if not key:
            print("[rainfall] KMA_SERVICE_KEY not set -> sine fallback")
            return fallback
        return KmaRainfallProvider(coords["lat"], coords["lng"], key,
                                   ttl_sec=cfg.get("ttl_sec", 300.0), fallback=fallback)
    if t == "mock":
        return MockRainfallProvider(peak_mm_h=cfg.get("peak", 25.0))
    return fallback  # sine (already constructed and passed in)
