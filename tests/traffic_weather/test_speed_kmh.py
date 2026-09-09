"""교통 속도 실측(km/h) — TrafficBehaviorTracker × core.calibration 연동
(2026-08-26, Phase 2).

## 왜 이 시험이 있나

`docs/202608260842/` 계획 §1에서 확인된 결함 시정. 예전에는
``service/runner.py``가 ``block.get("calib")``를 읽었는데 그 키를 채우는
코드가 어디에도 없어, 화면·이벤트·보고서(공공기관 제출)에 나가는 "평균속도
N km/h"가 전부 고정계수(mpp=0.06, 즉 픽셀속도×0.216)로 나온 **근거 없는
값**이었다.

지켜야 할 것.

* **보정 안 된 지점은 km/h 를 None 으로 준다** — 가짜 값을 다시 만들지 않는다
* **px/s 판정 문턱(stall_speed·slow_speed)은 절대 안 바뀐다** — km/h 는
  표시용으로만 곁들인다. 바뀌면 기존 정체·정지 판정이 통째로 어긋난다
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import calibration as C
from tot_dashboard.models import Detection
from tot_dashboard.traffic_weather.perception.traffic_tracker import (
    TrafficBehaviorTracker,
)

# 화면 100x100px == 실제 10m x 10m (tests/core/test_calibration.py 의 SIMPLE과 동일).
CAL = C.from_dict({
    "ground": {
        "image_points": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "world_points": [[0, 0], [10, 0], [10, 10], [0, 10]],
    },
})


def _det(cx, cy, w=10.0, h=10.0, cls="car"):
    return Detection(cls_name=cls, confidence=0.9,
                     bbox=(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))


def _feed(tracker, xs, *, dt=1.0, y=50.0):
    """x 좌표 목록을 dt 초 간격으로 먹여 트랙 속도가 안정될 때까지 돌린다.
    마지막 update() 의 TrafficMetrics 를 돌려준다."""
    m = None
    for i, x in enumerate(xs):
        m = tracker.update([_det(x, y)], t=i * dt)
    return m


def test_보정_안_된_지점은_속도를_None으로_준다():
    """cal 을 안 주면(기본값) km/h 는 어디서도 나오면 안 된다 — 예전
    가짜 고정계수(mpp=0.06)를 되살리지 않는다."""
    tracker = TrafficBehaviorTracker()  # cal 없음 → 미보정
    m = _feed(tracker, [0, 20, 40, 60, 80])
    assert m.mean_speed_kmh is None
    assert m.mean_speed > 0  # px/s 는 여전히 정상적으로 난다
    assert tracker.last_objects
    assert all(v.speed_kmh is None for v in tracker.last_objects)


def test_보정하면_실측_km_h가_나온다():
    tracker = TrafficBehaviorTracker(cal=CAL)
    m = _feed(tracker, [0, 20, 40, 60, 80])
    # 20px/1s(등속) == SIMPLE 환산 2m/s == 7.2km/h
    assert m.mean_speed_kmh == pytest.approx(7.2, rel=0.05)
    assert tracker.last_objects[0].speed_kmh == pytest.approx(7.2, rel=0.05)


def test_px속도_판정문턱은_보정_후에도_바뀌지_않는다():
    """stall_speed/slow_speed 는 px/s 기준이다. 캘리브레이션을 줘도 정체·
    정지 판정과 mean_speed(px/s) 값 자체가 달라지면 안 된다."""
    xs = [0, 5, 10, 15, 20]  # 등속 5px/s — 기본 stall_speed(15px/s)보다 느림
    no_cal = _feed(TrafficBehaviorTracker(), xs)
    with_cal = _feed(TrafficBehaviorTracker(cal=CAL), xs)
    assert no_cal.stalled == with_cal.stalled == 1
    assert no_cal.state == with_cal.state
    assert no_cal.mean_speed == pytest.approx(with_cal.mean_speed, rel=0.01)
    assert with_cal.mean_speed_kmh is not None
    assert no_cal.mean_speed_kmh is None


def test_아주_짧은_dt에서도_px_s와_km_h가_같은_문턱을_쓴다():
    """dt가 1e-3보다 작으면 sp_obj(px/s)·sp_kmh 둘 다 같은 문턱(1e-3)으로
    눌러야 한다 — 하나만 누르면 km/h 만 비정상적으로 튀는 값이 나와
    "가짜 값을 없앤다"는 Phase 2 취지에 어긋난다."""
    tracker = TrafficBehaviorTracker(cal=CAL)
    tracker.update([_det(0, 50)], t=0.0)
    m = tracker.update([_det(30, 50)], t=0.0001)  # 실제 dt 는 1e-3보다 훨씬 작다
    v = tracker.last_objects[0]
    # sp_obj 는 1e-3 문턱으로 눌려 30px/0.001s = 30000 px/s.
    assert v.speed == pytest.approx(30000.0, rel=0.01)
    # km/h 도 같은 문턱(dt=0.001)을 써야 한다 — CAL: 30px==3m,
    # 3m/0.001s*3.6 = 10800km/h. raw dt(0.0001)를 그대로 썼다면 10배(108000)가 나온다.
    assert v.speed_kmh == pytest.approx(10800.0, rel=0.01)


def test_보정_전후_모두_기존_ROI_대수_판정과_함께_동작한다():
    """congestion_roi 판정 로직(test_congestion_roi.py)과 cal 이 서로
    간섭하지 않는지 확인한다."""
    left_half = [[[0, 0], [50, 0], [50, 100], [0, 100]]]
    tracker = TrafficBehaviorTracker(
        cal=CAL, congestion_roi=left_half, roi_frame_wh=(100, 100))
    # ROI 안(x<50)에서 시작해 밖으로 나가는 차 1대
    m = _feed(tracker, [0, 10, 20, 30, 40], y=50.0)
    assert m.n_vehicles == 1
    assert m.mean_speed_kmh is not None
