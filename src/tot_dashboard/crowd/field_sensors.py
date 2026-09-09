"""인파 위험행동 탐지용 현장 장비(스테레오 CCTV·환경센서) provider.

## 왜 provider 추상화인가

사양이 요구하는 현장 장비(Stereo CCTV, 환경센서)를 **아직 보유하지 않은** 상태에서
기능을 개발해야 한다. 그래서 ``traffic_weather/perception/rainfall_provider.py``가
이미 쓰고 있는 검증된 패턴(``blocks.json``의 ``rainfall.type: sine|mock|kma``)을
그대로 따라, **같은 인터페이스의 두 구현을 설정으로 고를 수 있게** 했다.

  - ``mock``   : 임시 데이터로 동작 (장비 없이 지금 개발·시연 가능)
  - ``device`` : 실제 장비가 있다고 가정한 연동 경로 (장비 설치 후 설정만 변경)

핵심은 **상위 로직(행동 판정·위험 추론)이 어느 쪽인지 몰라도 되게** 하는 것이다.
장비를 들이는 시점에 설정 한 줄만 바꾸면 되고, 판정 코드는 손대지 않는다.

⚠️ **2D LiDAR은 사용자 결정으로 범위에서 제외**되었다(docs/crowd_behavior_detection_plan.md).
따라서 군집 거리는 스테레오 깊이 또는 영상 기반 추정으로만 산출한다.

⚠️ ``device`` 구현은 **실제 장비로 검증된 적이 없다.** 장비 규격(프로토콜·엔드포인트·
응답 포맷)이 확정되면 ``_fetch()`` 부분을 실제 규격에 맞춰 교체해야 한다. 지금은
"HTTP JSON을 반환하는 장비"라는 일반적 가정으로 작성돼 있고, 연결 실패 시 조용히
mock으로 폴백하지 않고 **명시적으로 알린 뒤 폴백**한다(가짜 데이터가 실측으로
오인되는 것을 막기 위함 — 침수 도메인에서 겪은 문제).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


# ─────────────────────────────────────────────────────────────
# 자료구조
# ─────────────────────────────────────────────────────────────
@dataclass
class EnvironmentState:
    """환경센서 관측값. 사양 ①의 '환경 센서(야간·기온·강수 등)'."""
    t_sec: float
    is_night: bool
    temp_c: float | None = None
    rain_mm_h: float | None = None
    illuminance_lux: float | None = None
    source: str = "mock"

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_night": self.is_night, "temp_c": self.temp_c,
            "rain_mm_h": self.rain_mm_h, "illuminance_lux": self.illuminance_lux,
            "source": self.source,
        }


@dataclass
class DepthState:
    """스테레오 CCTV 깊이 정보. 사양 ①의 'Stereo CCTV'.

    ``person_distances_m``: 검출된 사람별 카메라 거리(m). 군집 간격 산출용.
    ``mean_spacing_m``: 인접 인물 간 평균 간격(m). 밀집도를 물리 단위로 표현.

    ⚠️ 이 값들은 **LiDAR 제외로 인해 정확도가 제한적**이다. 스테레오 깊이는
    조도·텍스처에 민감하며, mock 모드에서는 애초에 합성값이다. 절대 밀집도
    기준(예: ㎡당 인원)이 필요한 사업 요구가 있다면 별도 검증이 필요하다.
    """
    t_sec: float
    person_distances_m: list[float] = field(default_factory=list)
    mean_spacing_m: float | None = None
    source: str = "mock"

    def to_dict(self) -> dict[str, Any]:
        return {
            "person_count": len(self.person_distances_m),
            "mean_spacing_m": self.mean_spacing_m,
            "source": self.source,
        }


# ─────────────────────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────────────────────
class EnvironmentProvider(Protocol):
    def at(self, t_sec: float) -> EnvironmentState: ...


class DepthProvider(Protocol):
    def at(self, t_sec: float, boxes: Any = None) -> DepthState: ...


# ─────────────────────────────────────────────────────────────
# mock 구현 — 장비 없이 개발·시연
# ─────────────────────────────────────────────────────────────
class MockEnvironmentProvider:
    """임시 환경 데이터.

    야간 판정은 **실제 시각**을 쓴다(합성이 아님) — 시각은 장비 없이도 정확히
    알 수 있고, 사양의 핵심 규칙(``NightTime = True``)이 여기 의존하기 때문이다.
    기온·조도만 합성한다.
    """

    def __init__(self, night_start: int = 20, night_end: int = 6,
                 base_temp_c: float = 18.0, rain_mm_h: float = 0.0):
        self.night_start = night_start
        self.night_end = night_end
        self.base_temp_c = base_temp_c
        self.rain_mm_h = rain_mm_h

    def at(self, t_sec: float) -> EnvironmentState:
        hour = datetime.now().hour
        is_night = hour >= self.night_start or hour < self.night_end
        # 일중 기온 변화를 코사인으로 근사(새벽 최저 ~ 오후 최고)
        temp = self.base_temp_c + 6.0 * math.cos((hour - 15) / 24 * 2 * math.pi)
        lux = 5.0 if is_night else 12000.0
        return EnvironmentState(t_sec=round(t_sec, 2), is_night=is_night,
                                temp_c=round(temp, 1), rain_mm_h=self.rain_mm_h,
                                illuminance_lux=lux, source="mock")


class MockDepthProvider:
    """임시 깊이 데이터.

    검출 박스가 주어지면 **박스 높이로 거리를 근사**한다(멀수록 작게 보임).
    이는 단순한 원근 가정이며 스테레오 실측이 아니다 — 그래도 완전 난수보다는
    실제 장면과 상관이 있어 상위 로직 검증에 쓸 만하다.
    """

    def __init__(self, ref_box_height_px: float = 200.0, ref_distance_m: float = 5.0):
        self.ref_h = ref_box_height_px
        self.ref_d = ref_distance_m

    def at(self, t_sec: float, boxes: Any = None) -> DepthState:
        dists: list[float] = []
        if boxes is not None and len(boxes):
            for b in boxes:
                h_px = float(b[3]) - float(b[1])
                if h_px > 1:
                    dists.append(round(self.ref_d * self.ref_h / h_px, 2))
        spacing = None
        if len(dists) >= 2:
            s = sorted(dists)
            gaps = [s[i + 1] - s[i] for i in range(len(s) - 1)]
            spacing = round(sum(gaps) / len(gaps), 2)
        return DepthState(t_sec=round(t_sec, 2), person_distances_m=dists,
                          mean_spacing_m=spacing, source="mock")


# ─────────────────────────────────────────────────────────────
# device 구현 — 실제 장비가 있다고 가정
# ─────────────────────────────────────────────────────────────
class _HttpDeviceBase:
    """HTTP JSON을 반환하는 현장 장비에 대한 공통 조회부(TTL 캐시 포함).

    ⚠️ 실제 장비 규격 확정 전의 **일반적 가정**이다. 규격이 나오면 이 클래스만
    교체하면 되도록 상위 provider와 분리했다.
    """

    def __init__(self, url: str, timeout: float = 2.0, ttl_sec: float = 5.0,
                 api_key: str | None = None):
        self.url = url
        self.timeout = timeout
        self.ttl = ttl_sec
        self.api_key = api_key
        self._cache: dict | None = None
        self._cache_t = 0.0
        self._warned = False

    def _fetch(self) -> dict | None:
        now = time.time()
        if self._cache is not None and (now - self._cache_t) < self.ttl:
            return self._cache
        try:
            import requests
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            r = requests.get(self.url, timeout=self.timeout, headers=headers)
            r.raise_for_status()
            self._cache = r.json()
            self._cache_t = now
            self._warned = False
            return self._cache
        except Exception as e:  # noqa: BLE001
            if not self._warned:
                # 한 번만 경고 -- 장비 장애 시 로그가 폭주하지 않도록
                print(f"[field_sensors] 장비 조회 실패({self.url}): {str(e)[:100]}")
                print("[field_sensors]   -> mock 폴백. 이 값은 실측이 아닙니다.")
                self._warned = True
            return None


class DeviceEnvironmentProvider(_HttpDeviceBase):
    """실제 환경센서 연동. 실패 시 mock 폴백(명시적 경고 후)."""

    def __init__(self, url: str, fallback: EnvironmentProvider, **kw):
        super().__init__(url, **kw)
        self.fallback = fallback

    def at(self, t_sec: float) -> EnvironmentState:
        d = self._fetch()
        if d is None:
            return self.fallback.at(t_sec)
        return EnvironmentState(
            t_sec=round(t_sec, 2),
            is_night=bool(d.get("is_night", False)),
            temp_c=d.get("temp_c"), rain_mm_h=d.get("rain_mm_h"),
            illuminance_lux=d.get("illuminance_lux"),
            source="device",
        )


class DeviceDepthProvider(_HttpDeviceBase):
    """실제 스테레오 CCTV 깊이 연동. 실패 시 mock 폴백(명시적 경고 후)."""

    def __init__(self, url: str, fallback: DepthProvider, **kw):
        super().__init__(url, **kw)
        self.fallback = fallback

    def at(self, t_sec: float, boxes: Any = None) -> DepthState:
        d = self._fetch()
        if d is None:
            return self.fallback.at(t_sec, boxes)
        dists = [float(x) for x in (d.get("person_distances_m") or [])]
        return DepthState(
            t_sec=round(t_sec, 2), person_distances_m=dists,
            mean_spacing_m=d.get("mean_spacing_m"), source="device",
        )


# ─────────────────────────────────────────────────────────────
# 팩토리 — blocks.json 설정 -> provider
# ─────────────────────────────────────────────────────────────
def build_environment(cfg: dict | None) -> EnvironmentProvider:
    """``environment`` 설정 -> provider. type: mock(기본) | device.

    device 선택 시 ``url``이 없으면 mock으로 되돌린다(설정 실수 방어).
    """
    cfg = cfg or {"type": "mock"}
    mock = MockEnvironmentProvider(
        night_start=cfg.get("night_start", 20), night_end=cfg.get("night_end", 6),
        base_temp_c=cfg.get("base_temp_c", 18.0), rain_mm_h=cfg.get("rain_mm_h", 0.0))
    if cfg.get("type") == "device":
        url = cfg.get("url")
        if not url:
            print("[field_sensors] environment.type=device 인데 url 없음 -> mock 사용")
            return mock
        return DeviceEnvironmentProvider(
            url, fallback=mock, ttl_sec=cfg.get("ttl_sec", 5.0),
            api_key=os.environ.get(cfg.get("api_key_env", "")) or None)
    return mock


def build_depth(cfg: dict | None) -> DepthProvider:
    """``stereo`` 설정 -> provider. type: mock(기본) | device."""
    cfg = cfg or {"type": "mock"}
    mock = MockDepthProvider(
        ref_box_height_px=cfg.get("ref_box_height_px", 200.0),
        ref_distance_m=cfg.get("ref_distance_m", 5.0))
    if cfg.get("type") == "device":
        url = cfg.get("url")
        if not url:
            print("[field_sensors] stereo.type=device 인데 url 없음 -> mock 사용")
            return mock
        return DeviceDepthProvider(
            url, fallback=mock, ttl_sec=cfg.get("ttl_sec", 5.0),
            api_key=os.environ.get(cfg.get("api_key_env", "")) or None)
    return mock
