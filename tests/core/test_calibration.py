"""지점별 캘리브레이션 (core/calibration.py).

지켜야 할 것.

* **보정하지 않았으면 보정한 척하지 않는다** — 모든 변환이 ``None`` 을 돌려주고
  단계는 「미보정」이 된다. 0 이나 추정값을 주면 화면이 거짓말한다
* **원근을 실제로 푼다** — 화면 위쪽 1픽셀이 아래쪽보다 넓다. 같은 픽셀 면적을
  같은 ㎡로 세면 캘리브레이션을 한 의미가 없다
* **관측 범위 밖은 외삽하지 않는다** — 근거 없는 깊이로 통제를 권고하면 안 된다
* **설정이 깨져도 탐지가 멈추지 않는다** — 항목 단위로 무시한다
* **기준값은 외부 근거에서 온 것** — 3·4·5명/㎡, 5·15·30cm
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import calibration as C

# 화면 100x100 px 이 실제 10m x 10m 인 단순한 경우
SIMPLE = {
    "ground": {
        "image_points": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "world_points": [[0, 0], [10, 0], [10, 10], [0, 10]],
    }
}


# --- 미보정 ------------------------------------------------------------------
def test_보정이_없으면_전부_None():
    cal = C.from_dict(None)
    assert cal.any is False
    assert C.crowd_per_m2(cal, 10, [[0, 0], [1, 0], [1, 1]]) is None
    assert C.flood_depth_cm(cal, 0.3) is None
    assert C.road_per_100m(cal, 5) is None
    assert C.traffic_speed_kmh(cal, [0, 0], [10, 0], 1.0) is None


def test_미보정은_단계도_미보정():
    """0 이나 「관심」으로 표시하면 보정된 것처럼 보인다."""
    assert C.crowd_level(None) == "미보정"
    assert C.flood_level(None) == "미보정"
    assert C.road_level(None) == "미보정"


# --- 지면 평면 ---------------------------------------------------------------
def test_화면_면적을_실제_제곱미터로_바꾼다():
    cal = C.from_dict(SIMPLE)
    area = cal.ground.area_m2([[0, 0], [100, 0], [100, 100], [0, 100]])
    assert area == pytest.approx(100.0, rel=0.01)   # 10m x 10m


def test_절반_면적은_절반_제곱미터():
    cal = C.from_dict(SIMPLE)
    area = cal.ground.area_m2([[0, 0], [50, 0], [50, 100], [0, 100]])
    assert area == pytest.approx(50.0, rel=0.01)


def test_원근이_있으면_같은_픽셀_면적도_다른_제곱미터가_된다():
    """이게 안 되면 캘리브레이션을 한 의미가 없다.

    사다리꼴(위가 좁은 화면)을 직사각형 바닥에 대응시키면, 화면 위쪽의 같은
    픽셀 넓이가 실제로는 더 넓은 바닥을 덮는다.
    """
    cal = C.from_dict({"ground": {
        # 화면: 위쪽이 좁은 사다리꼴 (멀리 있는 곳)
        "image_points": [[40, 0], [60, 0], [100, 100], [0, 100]],
        # 실제: 정사각형 10m x 10m
        "world_points": [[0, 0], [10, 0], [10, 10], [0, 10]],
    }})
    윗쪽 = cal.ground.area_m2([[40, 0], [60, 0], [60, 20], [40, 20]])
    아래쪽 = cal.ground.area_m2([[40, 80], [60, 80], [60, 100], [40, 100]])
    assert 윗쪽 is not None and 아래쪽 is not None
    assert 윗쪽 > 아래쪽 * 1.5, "원근이 반영되지 않았다"


def test_네_점이_일직선이면_거부한다():
    errors = C.validate({"ground": {
        "image_points": [[0, 0], [10, 0], [20, 0], [30, 0]],
        "world_points": [[0, 0], [1, 0], [2, 0], [3, 0]],
    }})
    assert errors


def test_점이_4개가_아니면_거부한다():
    errors = C.validate({"ground": {
        "image_points": [[0, 0], [10, 0], [20, 0]],
        "world_points": [[0, 0], [1, 0], [2, 0]],
    }})
    assert any("4점" in e for e in errors)


# --- 인파 밀도 ---------------------------------------------------------------
def test_인파_밀도를_명당_제곱미터로_낸다():
    cal = C.from_dict(SIMPLE)
    # 100㎡ 에 300명 → 3명/㎡
    d = C.crowd_per_m2(cal, 300, [[0, 0], [100, 0], [100, 100], [0, 100]])
    assert d == pytest.approx(3.0, rel=0.01)


@pytest.mark.parametrize("per_m2, expected", [
    (1.0, "관심"),
    (3.0, "주의"),     # [외부] 국내 기준
    (4.0, "경계"),     # [외부] 국내 · 영국 이동 대기열 한계
    (5.0, "심각"),     # [외부] 국제 압사 임계
    (11.0, "심각"),    # 이태원 참사 수준
])
def test_국내외_기준으로_단계를_나눈다(per_m2, expected):
    assert C.crowd_level(per_m2) == expected


def test_기준값이_외부_근거와_일치한다():
    """이 숫자를 바꾸면 근거 문서도 함께 고쳐야 한다."""
    assert (C.CROWD_M2_CAUTION, C.CROWD_M2_ALERT, C.CROWD_M2_CRITICAL) == (3.0, 4.0, 5.0)
    assert (C.FLOOD_CM_CONTROL, C.FLOOD_CM_TRACTION, C.FLOOD_CM_FLOAT) == (5.0, 15.0, 30.0)


# --- 교통 속도(km/h) ----------------------------------------------------------
# (2026-08-26, Phase 2 — docs/202608260842/ §1 결함 시정: 예전 화면의
#  km/h는 block["calib"]["mpp"]를 채우는 코드가 없어 항상 고정계수
#  mpp=0.06 로 떨어진 가짜값이었다. 이제 GroundPlane.to_world()로
#  실측한다.)

def test_지면_보정이_있으면_속도를_실측한다():
    # SIMPLE: 화면 100x100px == 실제 10m x 10m. 화면에서 100px 이동 == 10m.
    cal = C.from_dict(SIMPLE)
    # 10m 를 1초에 이동 → 36 km/h
    kmh = C.traffic_speed_kmh(cal, [0, 0], [100, 0], 1.0)
    assert kmh == pytest.approx(36.0, rel=0.01)


def test_이동거리가_절반이면_속도도_절반():
    cal = C.from_dict(SIMPLE)
    kmh = C.traffic_speed_kmh(cal, [0, 0], [50, 0], 1.0)
    assert kmh == pytest.approx(18.0, rel=0.01)


def test_dt가_0이하면_None():
    """나눗셈 0 이나 음수 시간으로 무의미한 속도를 내지 않는다."""
    cal = C.from_dict(SIMPLE)
    assert C.traffic_speed_kmh(cal, [0, 0], [100, 0], 0.0) is None
    assert C.traffic_speed_kmh(cal, [0, 0], [100, 0], -1.0) is None


def test_원근이_있어도_실제_거리로_잰다():
    """meters_per_pixel() 대신 to_world() 로 두 점을 각각 옮겨 재므로,
    화면 위쪽(먼 곳)의 같은 픽셀 이동이 실제로는 더 먼 거리로 나와야 한다."""
    cal = C.from_dict({"ground": {
        "image_points": [[40, 0], [60, 0], [100, 100], [0, 100]],
        "world_points": [[0, 0], [10, 0], [10, 10], [0, 10]],
    }})
    위쪽_kmh = C.traffic_speed_kmh(cal, [40, 0], [60, 0], 1.0)
    아래쪽_kmh = C.traffic_speed_kmh(cal, [0, 100], [20, 100], 1.0)
    assert 위쪽_kmh is not None and 아래쪽_kmh is not None
    assert 위쪽_kmh > 아래쪽_kmh, "원근이 반영되지 않았다"


# --- 침수심 ------------------------------------------------------------------
DEPTH = {"depth": {"points": [[0.0, 0.0], [0.10, 5.0], [0.30, 20.0]]}}


def test_관측점_사이는_보간한다():
    cal = C.from_dict(DEPTH)
    # 0.10 → 5cm, 0.30 → 20cm 사이의 0.20 은 12.5cm
    assert C.flood_depth_cm(cal, 0.20) == pytest.approx(12.5, rel=0.01)


def test_관측점_위에서는_그_값():
    cal = C.from_dict(DEPTH)
    assert C.flood_depth_cm(cal, 0.10) == pytest.approx(5.0)


def test_관측_범위_밖은_외삽하지_않는다():
    """근거 없는 깊이로 통제를 권고하면 안 된다."""
    cal = C.from_dict(DEPTH)
    assert C.flood_depth_cm(cal, 0.90) == pytest.approx(20.0), "외삽했다"
    assert cal.depth.saturated(0.90) is True
    assert cal.depth.saturated(0.20) is False


def test_관측점이_하나면_보정으로_치지_않는다():
    cal = C.from_dict({"depth": {"points": [[0.1, 5.0]]}})
    assert cal.depth is None
    assert C.flood_depth_cm(cal, 0.2) is None


@pytest.mark.parametrize("cm, expected", [
    (1.0, "관심"),
    (5.0, "주의"),        # [외부] 국내 지하차도 통제
    (15.0, "경계"),       # [외부] 차량 접지력 상실
    (30.0, "심각"),       # [외부] 소형차 부유
    (60.0, "심각"),
])
def test_침수심_기준으로_단계를_나눈다(cm, expected):
    assert C.flood_level(cm) == expected


def test_면적비가_0에서_1을_벗어나면_거부한다():
    assert C.validate({"depth": {"points": [[1.5, 5.0], [0.2, 3.0]]}})


def test_같은_면적비가_두_번이면_거부한다():
    assert C.validate({"depth": {"points": [[0.2, 5.0], [0.2, 9.0]]}})


# --- 노면 구간 ---------------------------------------------------------------
def test_구간_길이로_100미터당_건수를_낸다():
    cal = C.from_dict({"section": {"length_m": 200.0}})
    assert C.road_per_100m(cal, 6) == pytest.approx(3.0)


def test_긴_구간이_불리하지_않다():
    """개수만 세면 긴 구간이 항상 나빠 보인다. 그래서 구간 단위로 바꿨다."""
    짧은 = C.from_dict({"section": {"length_m": 50.0}})
    긴 = C.from_dict({"section": {"length_m": 500.0}})
    assert C.road_per_100m(짧은, 2) > C.road_per_100m(긴, 5)


def test_구간_길이가_0이면_보정으로_치지_않는다():
    cal = C.from_dict({"section": {"length_m": 0}})
    assert cal.section is None


def test_구간_길이가_비상식적이면_거부한다():
    assert C.validate({"section": {"length_m": 50000}})
    assert C.validate({"section": {"length_m": -1}})


# --- 깨진 설정 ---------------------------------------------------------------
@pytest.mark.parametrize("broken", [
    {"ground": "문자열"},
    {"ground": {"image_points": "x", "world_points": "y"}},
    {"depth": {"points": [["가", "나"]]}},
    {"section": {"length_m": "백미터"}},
    {"ground": {"image_points": [[0, 0]], "world_points": [[0, 0]]}},
])
def test_설정이_깨져도_예외를_내지_않는다(broken):
    """설정 하나 때문에 탐지가 멈추면 안 된다."""
    cal = C.from_dict(broken)
    assert isinstance(cal, C.Calibration)


def test_한_항목이_깨져도_나머지는_산다():
    cal = C.from_dict({"ground": "깨짐", "section": {"length_m": 100.0}})
    assert cal.ground is None
    assert cal.section is not None


# --- 저장 형식 ---------------------------------------------------------------
def test_저장했다_읽으면_같다():
    cal = C.from_dict({**SIMPLE, **DEPTH, "section": {"length_m": 120.0}})
    again = C.from_dict(cal.to_dict())
    assert again.ground.world_points == cal.ground.world_points
    assert again.depth.points == cal.depth.points
    assert again.section.length_m == cal.section.length_m
