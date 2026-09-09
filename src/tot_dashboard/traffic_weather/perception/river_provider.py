"""★ 2026-08-21 이전됨 — 실제 구현은 ``flood/river_provider.py``에 있습니다.

하천 수위는 직접 침수 신호라 flood/traffic 도메인 분리(docs/202608210801)
때 flood 소유로 옮겼습니다. 이 파일은 기존 import 경로(``from
..traffic_weather.perception.river_provider import build_river``)가 계속
동작하도록 남겨 둔 얇은 재-export shim입니다 — 새 코드는
``tot_dashboard.flood.river_provider``를 직접 import하십시오.
"""
from __future__ import annotations

from ...flood.river_provider import (  # noqa: F401
    HrfcoRiverProvider,
    MockRiverProvider,
    NoRiverProvider,
    RiverProvider,
    build_river,
)
