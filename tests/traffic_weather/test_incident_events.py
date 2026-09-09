"""교통 돌발상황 탐지(``incident_events.TrafficIncidentDetector``) — 2026-08-26 신설.

Phase 1(보행자 도로 진입) + Phase 4(역주행 의심). 사고 의심은 각자의
회차에서 같은 파일에 더한다.
"""
from __future__ import annotations

from tot_dashboard.models import VehicleObject
from tot_dashboard.traffic_weather.perception.incident_events import (
    TrafficIncidentDetector)

# 화면(1000x1000 가정)의 왼쪽 절반을 도로 ROI로, 오른쪽 위 작은 사각형을
# 횡단보도 제외구역으로 둔다.
ROAD_ROI = [[[0, 0], [500, 0], [500, 1000], [0, 1000]]]
EXEMPT_ROI = [[[100, 100], [300, 100], [300, 300], [100, 300]]]

ON_ROAD = (200, 500)          # 도로 ROI 안, 제외구역 밖
IN_EXEMPT = (200, 200)        # 도로 ROI 안 이면서 제외구역 안
OFF_ROAD = (800, 500)         # 도로 ROI 밖


def _detector(**kw) -> TrafficIncidentDetector:
    kw.setdefault("pedestrian_roi", ROAD_ROI)
    kw.setdefault("pedestrian_exempt_roi", EXEMPT_ROI)
    kw.setdefault("pedestrian_on_road_sec", 3.0)
    kw.setdefault("incident_hold_sec", 60.0)
    return TrafficIncidentDetector(**kw)


def test_도로_ROI_밖_보행자는_이벤트가_되지_않는다():
    d = _detector()
    for t in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0):
        d.update(t, vehicles=[], persons=[OFF_ROAD])
    assert d.active(5.0) == []


def test_한_프레임_깜빡임은_보행자_진입으로_보지_않는다():
    """도로 위 사람이 한 틱만 보이고 사라지면(오탐성 깜빡임) 사건이 안 된다."""
    d = _detector()
    d.update(0.0, vehicles=[], persons=[ON_ROAD])
    d.update(0.2, vehicles=[], persons=[])  # 바로 사라짐
    assert d.active(0.2) == []


def test_연속_관측_문턱을_넘으면_보행자_진입이_보고된다():
    d = _detector(pedestrian_on_road_sec=3.0)
    for t in (0.0, 1.0, 2.0, 2.9):
        d.update(t, vehicles=[], persons=[ON_ROAD])
        assert d.active(t) == [], f"t={t}: 아직 문턱(3초) 전인데 보고됨"
    d.update(3.0, vehicles=[], persons=[ON_ROAD])
    active = d.active(3.0)
    assert len(active) == 1
    assert active[0].code == "TIN_PEDESTRIAN"
    assert active[0].hazard_type_code == "traffic_pedestrian"


def test_횡단보도_제외구역_안의_보행자는_무시한다():
    """신호 대기 중인 보행자가 상시 발동하는 것을 막는다."""
    d = _detector()
    for t in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0):
        d.update(t, vehicles=[], persons=[IN_EXEMPT])
    assert d.active(5.0) == []


def test_같은_연속관측에서는_한_번만_보고한다():
    d = _detector(pedestrian_on_road_sec=3.0)
    for t in [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]:
        d.update(t, vehicles=[], persons=[ON_ROAD])
    # active() 는 hold 창 동안 계속 같은 사건을 돌려주지만, _held 에 쌓인
    # "발생 건수" 자체는 1건이어야 한다(같은 연속 관측에서 재보고 안 함).
    assert len(d._held) == 1


def test_사건은_hold창_동안_계속_보인다():
    """event_sync 20초 폴링이 순간 사건을 놓치지 않으려면, 사건이 발생한
    뒤 incident_hold_sec 동안은 active() 가 계속 돌려줘야 한다."""
    d = _detector(pedestrian_on_road_sec=1.0, incident_hold_sec=10.0)
    d.update(0.0, vehicles=[], persons=[ON_ROAD])
    d.update(1.0, vehicles=[], persons=[ON_ROAD])  # 여기서 발생
    d.update(2.0, vehicles=[], persons=[])  # 사람은 이미 떠났지만
    assert len(d.active(5.0)) == 1          # hold 창(10초) 안이라 아직 보임
    assert len(d.active(11.5)) == 0         # 창이 지나면 사라진다


def test_떠났다가_다시_들어오면_새_사건이_될_수_있다():
    d = _detector(pedestrian_on_road_sec=1.0, incident_hold_sec=5.0)
    d.update(0.0, vehicles=[], persons=[ON_ROAD])
    d.update(1.0, vehicles=[], persons=[ON_ROAD])  # 1차 발생
    d.update(2.0, vehicles=[], persons=[])          # 완전히 떠남
    d.update(10.0, vehicles=[], persons=[ON_ROAD])  # 다시 들어옴(hold 지난 뒤)
    d.update(11.0, vehicles=[], persons=[ON_ROAD])  # 2차 발생
    assert len(d._held) == 2


def test_해상도가_다르면_ROI_좌표를_보정한다():
    """ROI는 정지영상 해상도로 저장되고 실제 프레임은 다를 수 있다
    (traffic_tracker._roi_polygons 와 같은 함정)."""
    # 정지영상 1000x1000 기준 ROI를, 실제 프레임 500x500(절반 축소)에서 씀.
    d = TrafficIncidentDetector(
        pedestrian_roi=ROAD_ROI, roi_frame_wh=(1000, 1000),
        pedestrian_on_road_sec=1.0)
    scaled_on_road = (100, 250)  # ON_ROAD(200,500)의 절반
    for t in (0.0, 1.0):
        d.update(t, vehicles=[], persons=[scaled_on_road], frame_wh=(500, 500))
    assert len(d.active(1.0)) == 1


# --- 역주행 의심 (Phase 4, 2026-08-26) ----------------------------------------
#
# 화살표: (0,500) → (500,500), 즉 "정상 방향은 +x(오른쪽)".
FORWARD_ARROW = [[[0, 500], [500, 500]]]


def _veh(tid, x, y, w=10.0, h=10.0):
    """bbox 하단 중심이 정확히 (x, y)가 되도록 만든다."""
    return VehicleObject(track_id=tid, bbox=(x - w / 2, y - h, x + w / 2, y),
                         speed=0.0, speed_drop=0.0, age=1)


def _wrongway_detector(**kw):
    kw.setdefault("flow_arrows", FORWARD_ARROW)
    kw.setdefault("wrongway_min_sec", 2.0)
    kw.setdefault("heading_window_sec", 1.0)
    kw.setdefault("heading_min_disp_px", 8.0)
    kw.setdefault("wrongway_max_arrow_dist_px", 150.0)
    return TrafficIncidentDetector(**kw)


def _feed_moving(d, tid, x0, dx_per_tick, y=500.0, ticks=8, dt=0.5):
    t = 0.0
    for i in range(ticks):
        t = i * dt
        d.update(t, vehicles=[_veh(tid, x0 + dx_per_tick * i, y)],
                persons=[], frame_wh=None)
    return t


def test_화살표가_없으면_역주행_판정_자체를_하지_않는다():
    """기준(정상 방향)이 없으면 판정하지 않는다 — 억지 판정은 오탐만 낳는다."""
    d = _wrongway_detector(flow_arrows=[])
    t = _feed_moving(d, 1, x0=300, dx_per_tick=-20)  # 왼쪽(역방향)으로 계속 이동
    assert d.active(t) == []


def test_정상_방향_차량은_역주행이_아니다():
    d = _wrongway_detector()
    t = _feed_moving(d, 1, x0=100, dx_per_tick=20)  # 오른쪽 = 화살표와 같은 방향
    assert d.active(t) == []


def test_반대_방향이_지속되면_역주행으로_보고된다():
    d = _wrongway_detector()
    t = _feed_moving(d, 1, x0=400, dx_per_tick=-20)  # 왼쪽 = 화살표와 반대
    active = d.active(t)
    assert len(active) == 1
    assert active[0].code == "TIN_WRONGWAY"
    assert active[0].hazard_type_code == "traffic_wrongway"
    assert active[0].level.value == "경계"


def test_문턱_시간_전에는_보고하지_않는다():
    d = _wrongway_detector(wrongway_min_sec=2.0)
    # 반대 방향 이동을 1틱(0.5초)만 먹인다 — 지속시간 문턱(2초) 훨씬 전.
    t = _feed_moving(d, 1, x0=400, dx_per_tick=-20, ticks=2)
    assert d.active(t) == []


def test_정지_차량은_방향을_재지_않아_역주행이_아니다():
    """잡음 헤딩(픽셀 단위 흔들림)이 오탐으로 이어지면 안 된다."""
    d = _wrongway_detector(heading_min_disp_px=8.0)
    # 화살표와 반대쪽에 있지만 거의 안 움직인다(틱당 1px < 문턱 8px).
    t = _feed_moving(d, 1, x0=400, dx_per_tick=-1, ticks=10)
    assert d.active(t) == []


def test_화살표에서_너무_멀면_판정하지_않는다():
    """그 화살표의 "관할 밖"이면 근거 없이 판정하지 않는다."""
    d = _wrongway_detector(wrongway_max_arrow_dist_px=150.0)
    # 화살표는 y=500 부근인데 차량은 y=5000 부근 — 중점까지 거리가 문턱을 넘는다.
    t = _feed_moving(d, 1, x0=400, dx_per_tick=-20, y=5000.0)
    assert d.active(t) == []


def test_같은_트랙에서는_한_번만_보고한다():
    d = _wrongway_detector()
    t = _feed_moving(d, 1, x0=400, dx_per_tick=-20, ticks=16)  # 문턱을 한참 넘겨 계속
    # active() 는 hold 창 동안 계속 돌려주지만, 실제 "발생" 자체는 1건이어야
    # 한다 — held 안에 같은 트랙의 사건이 여러 번 쌓이면 안 된다.
    wrongway = [inc for _exp, inc in d._held if inc.code == "TIN_WRONGWAY"]
    assert len(wrongway) == 1


def test_해상도가_다르면_화살표_좌표도_보정한다():
    """flow_arrows 도 congestion_roi·pedestrian_roi 와 같은 함정을 겪는다
    (정지영상 해상도 ≠ 실제 프레임 해상도)."""
    # 정지영상 1000x1000 기준 화살표를, 실제 프레임 500x500(절반 축소)에서 씀.
    d = TrafficIncidentDetector(
        flow_arrows=FORWARD_ARROW, roi_frame_wh=(1000, 1000),
        wrongway_min_sec=1.0, heading_window_sec=1.0, heading_min_disp_px=4.0)
    t = 0.0
    for i in range(6):
        t = i * 0.5
        # 실제 프레임 좌표계(500x500)에서 왼쪽(반대 방향)으로 이동.
        d.update(t, vehicles=[_veh(1, 200 - 10 * i, 250)], persons=[],
                frame_wh=(500, 500))
    active = d.active(t)
    assert len(active) == 1
    assert active[0].code == "TIN_WRONGWAY"


# --- 사고 의심 (Phase 5, 2026-08-26) -------------------------------------------


def _stalled_veh(tid, x, y, w=10.0, h=10.0):
    return VehicleObject(track_id=tid, bbox=(x - w / 2, y - h, x + w / 2, y),
                         speed=0.0, speed_drop=0.9, age=1, stalled=True)


def _moving_veh(tid, x, y, w=10.0, h=10.0):
    return VehicleObject(track_id=tid, bbox=(x - w / 2, y - h, x + w / 2, y),
                         speed=100.0, speed_drop=0.0, age=1, stalled=False)


def _accident_detector(**kw):
    kw.setdefault("accident_stall_sec", 12.0)
    kw.setdefault("accident_cluster_px", 180.0)
    kw.setdefault("accident_min_cluster", 2)
    return TrafficIncidentDetector(**kw)


def _feed_stalled(d, vehicles_fn, *, ticks=14, dt=1.0, is_blocked=False):
    t = 0.0
    for i in range(ticks):
        t = i * dt
        d.update(t, vehicles=vehicles_fn(i), persons=[], is_blocked=is_blocked)
    return t


def test_뭉친_정지차량은_사고_의심으로_보고된다():
    d = _accident_detector(accident_stall_sec=12.0)
    t = _feed_stalled(d, lambda i: [_stalled_veh(1, 500, 500),
                                    _stalled_veh(2, 550, 500)])  # 50px 이내
    active = d.active(t)
    accidents = [a for a in active if a.code == "TIN_ACCIDENT"]
    assert len(accidents) == 1
    assert accidents[0].hazard_type_code == "traffic_accident"
    assert accidents[0].level.value == "경계"
    assert "2대" in accidents[0].evidence_text


def test_흩어진_정지차량은_사고로_보지_않는다():
    """★ 기존 stalled 집계(ROI 전체 카운트)로는 못 잡는 구분 — 흩어져
    있으면 사고 신호가 아니다."""
    d = _accident_detector(accident_cluster_px=180.0)
    t = _feed_stalled(d, lambda i: [_stalled_veh(1, 100, 100),
                                    _stalled_veh(2, 900, 900)])  # 아주 멀다
    assert d.active(t) == []


def test_군집_크기가_최소치_미만이면_보고하지_않는다():
    d = _accident_detector(accident_min_cluster=2)
    t = _feed_stalled(d, lambda i: [_stalled_veh(1, 500, 500)])  # 1대뿐
    assert d.active(t) == []


def test_문턱_시간_전에는_보고하지_않는다_사고():
    d = _accident_detector(accident_stall_sec=12.0)
    t = _feed_stalled(d, lambda i: [_stalled_veh(1, 500, 500),
                                    _stalled_veh(2, 550, 500)], ticks=5)  # 5초뿐
    assert d.active(t) == []


def test_도로_전체가_정지상태면_사고로_보지_않는다():
    """★ 정체/사고 구분자 — TrafficState.blocked 면 판정 자체를 안 한다."""
    d = _accident_detector()
    t = _feed_stalled(d, lambda i: [_stalled_veh(1, 500, 500),
                                    _stalled_veh(2, 550, 500)],
                      is_blocked=True)
    assert d.active(t) == []


def test_움직이기_시작하면_후보에서_빠진다():
    d = _accident_detector(accident_stall_sec=5.0)
    t = 0.0
    for i in range(3):
        t = i * 1.0
        d.update(t, vehicles=[_stalled_veh(1, 500, 500), _stalled_veh(2, 550, 500)],
                persons=[])
    # 문턱(5초) 전에 다시 움직이기 시작한다.
    for i in range(3, 10):
        t = i * 1.0
        d.update(t, vehicles=[_moving_veh(1, 500 + i * 20, 500),
                              _stalled_veh(2, 550, 500)], persons=[])
    assert d.active(t) == []


def test_같은_군집은_반복_보고하지_않는다():
    d = _accident_detector(accident_stall_sec=5.0)
    t = _feed_stalled(d, lambda i: [_stalled_veh(1, 500, 500),
                                    _stalled_veh(2, 550, 500)], ticks=20)
    accidents = [inc for _exp, inc in d._held if inc.code == "TIN_ACCIDENT"]
    assert len(accidents) == 1
