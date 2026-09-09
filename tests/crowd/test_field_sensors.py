"""현장 장비 provider(mock/device 전환) 및 배회·침입 탐지 테스트.

장비를 보유하지 않은 상태에서 개발했으므로, **mock 경로가 확실히 동작하고
device 경로가 실패 시 안전하게 폴백하는지**가 핵심 검증 대상이다.
"""
from __future__ import annotations

import numpy as np

from tot_dashboard.crowd.behavior_events import (EVENT_INTRUSION, EVENT_LOITERING,
                                                 BehaviorEventDetector)
from tot_dashboard.crowd.field_sensors import (DeviceDepthProvider,
                                               DeviceEnvironmentProvider,
                                               MockDepthProvider,
                                               MockEnvironmentProvider,
                                               build_depth, build_environment)


# ── provider 선택 ────────────────────────────────────────────
def test_default_is_mock():
    assert isinstance(build_environment(None), MockEnvironmentProvider)
    assert isinstance(build_depth(None), MockDepthProvider)


def test_device_selected_when_url_given():
    env = build_environment({"type": "device", "url": "http://10.0.0.9/env"})
    dep = build_depth({"type": "device", "url": "http://10.0.0.9/depth"})
    assert isinstance(env, DeviceEnvironmentProvider)
    assert isinstance(dep, DeviceDepthProvider)


def test_device_without_url_falls_back_to_mock():
    """설정 실수(url 누락)로 서비스가 죽지 않아야 한다."""
    assert isinstance(build_environment({"type": "device"}), MockEnvironmentProvider)
    assert isinstance(build_depth({"type": "device"}), MockDepthProvider)


def test_device_unreachable_falls_back_and_marks_source():
    """장비 연결 실패 시 mock 값을 쓰되, source가 'mock'으로 남아야 한다.

    가짜 데이터가 실측으로 오인되면 안 되기 때문에 source 표기가 중요하다.
    """
    env = build_environment({"type": "device", "url": "http://127.0.0.1:9/none",
                             "ttl_sec": 0.0})
    state = env.at(0.0)
    assert state.source == "mock"          # device 아님 -> 폴백된 것
    assert isinstance(state.is_night, bool)


# ── mock provider 동작 ───────────────────────────────────────
def test_mock_env_reports_boolean_night():
    s = MockEnvironmentProvider().at(0.0)
    assert isinstance(s.is_night, bool)
    assert s.source == "mock"
    assert s.illuminance_lux is not None


def test_mock_depth_from_box_height():
    """박스가 클수록(가까울수록) 거리가 짧게 나와야 한다."""
    dep = MockDepthProvider(ref_box_height_px=200.0, ref_distance_m=5.0)
    near = dep.at(0.0, boxes=np.array([[0, 0, 50, 400]]))     # 높이 400 -> 가까움
    far = dep.at(0.0, boxes=np.array([[0, 0, 50, 100]]))      # 높이 100 -> 멀리
    assert near.person_distances_m[0] < far.person_distances_m[0]


def test_mock_depth_empty_boxes():
    d = MockDepthProvider().at(0.0, boxes=np.zeros((0, 4)))
    assert d.person_distances_m == []
    assert d.mean_spacing_m is None


# ── 배회 탐지 ────────────────────────────────────────────────
def _still_box(x=100, y=100):
    return [x - 20, y - 60, x + 20, y]


def test_loitering_fires_after_threshold():
    det = BehaviorEventDetector(loiter_sec=10.0, block_id="BLOCK-A")
    boxes, ids = [_still_box()], [1]
    assert det.update(0.0, boxes, ids) == []          # 막 등장 -> 이벤트 없음
    evs = det.update(11.0, boxes, ids)                # 임계 초과
    assert len(evs) == 1
    assert evs[0].eventType == EVENT_LOITERING
    assert evs[0].dwellTimeSec >= 10.0
    assert evs[0].occursIn == "BLOCK-A"


def test_loitering_reported_only_once():
    det = BehaviorEventDetector(loiter_sec=5.0)
    boxes, ids = [_still_box()], [1]
    det.update(0.0, boxes, ids)
    assert len(det.update(6.0, boxes, ids)) == 1
    assert det.update(7.0, boxes, ids) == []          # 중복 경보 없음


def test_moving_person_is_not_loitering():
    """통행자는 오래 보여도 배회가 아니다(반경 조건)."""
    det = BehaviorEventDetector(loiter_sec=5.0, loiter_radius_px=50.0)
    for i, t in enumerate([0.0, 3.0, 6.0, 9.0]):
        evs = det.update(t, [_still_box(x=100 + i * 300)], [1])   # 크게 이동
    assert evs == []


def test_night_raises_loitering_confidence():
    class _Night:
        is_night = True

    class _Day:
        is_night = False

    def run(env):
        det = BehaviorEventDetector(loiter_sec=5.0, night_multiplier=1.5)
        det.update(0.0, [_still_box()], [1])
        return det.update(6.0, [_still_box()], [1], env=env)[0].confidence

    assert run(_Night()) > run(_Day())


# ── 침입 탐지 ────────────────────────────────────────────────
ROI = [[[0, 0], [200, 0], [200, 200], [0, 200]]]      # 좌상단 사각형


def test_intrusion_fires_on_entry():
    det = BehaviorEventDetector(intrusion_roi=ROI)
    outside = det.update(0.0, [_still_box(x=500, y=500)], [1])
    assert outside == []
    inside = det.update(1.0, [_still_box(x=100, y=100)], [1])
    assert len(inside) == 1
    assert inside[0].eventType == EVENT_INTRUSION


def test_intrusion_not_repeated_while_staying():
    det = BehaviorEventDetector(intrusion_roi=ROI, loiter_sec=9999)
    det.update(0.0, [_still_box(x=100, y=100)], [1])
    assert det.update(1.0, [_still_box(x=100, y=100)], [1]) == []


def test_no_intrusion_without_roi():
    det = BehaviorEventDetector(intrusion_roi=None, loiter_sec=9999)
    assert det.update(0.0, [_still_box(x=100, y=100)], [1]) == []


def test_already_inside_at_first_sight_is_not_intrusion():
    """회귀 테스트 -- 최초 관측 시점에 이미 ROI 안에 있으면 '진입'이 아니다.

    실동작 검증 중 발견한 버그: 분석 시작 순간 통제구역 안에 있던 사람들이
    전부 침입으로 잘못 보고됐다. 침입은 밖->안 '전환'일 때만 성립한다.
    """
    det = BehaviorEventDetector(intrusion_roi=ROI, loiter_sec=9999)
    # 첫 관측부터 ROI 내부
    assert det.update(0.0, [_still_box(x=100, y=100)], [1]) == []
    assert det.update(1.0, [_still_box(x=100, y=100)], [1]) == []


def test_intrusion_after_leaving_and_reentering_context():
    """밖에서 관측된 뒤 들어오면 정상적으로 침입으로 잡힌다."""
    det = BehaviorEventDetector(intrusion_roi=ROI, loiter_sec=9999)
    det.update(0.0, [_still_box(x=500, y=500)], [1])      # 밖
    evs = det.update(1.0, [_still_box(x=100, y=100)], [1])  # 안으로 진입
    assert len(evs) == 1 and evs[0].eventType == EVENT_INTRUSION


# ── 상태 관리 ────────────────────────────────────────────────
def test_stale_tracks_pruned():
    """오래 안 보인 추적은 정리된다(메모리 + 개인정보 최소보관)."""
    det = BehaviorEventDetector(track_ttl_sec=5.0, loiter_sec=9999)
    det.update(0.0, [_still_box()], [1])
    assert det.summary()["active_tracks"] == 1
    det.update(100.0, [_still_box(x=900)], [2])
    assert 1 not in det._tracks


def test_event_to_dict_shape():
    det = BehaviorEventDetector(loiter_sec=5.0, block_id="B", node_id="CAM-1")
    det.update(0.0, [_still_box()], [7])
    d = det.update(6.0, [_still_box()], [7])[0].to_dict()
    for k in ("eventType", "trackId", "confidence", "evidenceText",
              "occursIn", "detectedBy", "dwellTimeSec", "position"):
        assert k in d
    assert d["trackId"] == 7 and d["detectedBy"] == "CAM-1"


# --- 인파 ROI 3종 (2026-08-08 추가) -----------------------------------------
# 화면에 「침입 금지 구역」 하나뿐인 이유가 코드가 그것만 썼기 때문이었다.
# 분석 영역·배회 감시 구역을 추가하고, 실제로 판정에 반영되는지 확인한다.

def _square(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]]


def test_배회_감시_구역_밖은_배회로_보지_않는다():
    """정류장 대기줄처럼 오래 서 있는 것이 정상인 곳을 빼내기 위한 장치."""
    from tot_dashboard.crowd.behavior_events import (EVENT_LOITERING,
                                                     BehaviorEventDetector)
    zone = _square(0, 0, 100, 100)          # 좌상단 구석만 감시
    det = BehaviorEventDetector(loiter_sec=1.0, loiter_radius_px=50.0,
                                loiter_roi=zone)
    # 구역 밖(500, 500)에 오래 서 있다
    box = [480, 400, 520, 500]
    det.update(0.0, [box], [1])
    evs = det.update(5.0, [box], [1])
    assert not [e for e in evs if e.eventType == EVENT_LOITERING]


def test_배회_감시_구역_안이면_배회로_잡는다():
    from tot_dashboard.crowd.behavior_events import (EVENT_LOITERING,
                                                     BehaviorEventDetector)
    det = BehaviorEventDetector(loiter_sec=1.0, loiter_radius_px=50.0,
                                loiter_roi=_square(0, 0, 100, 100))
    box = [40, 0, 60, 50]                   # 발점 (50, 50) — 구역 안
    det.update(0.0, [box], [1])
    evs = det.update(5.0, [box], [1])
    assert [e for e in evs if e.eventType == EVENT_LOITERING]


def test_감시_구역을_지정하지_않으면_화면_전체에서_본다():
    """기존 동작이 바뀌면 안 된다."""
    from tot_dashboard.crowd.behavior_events import (EVENT_LOITERING,
                                                     BehaviorEventDetector)
    det = BehaviorEventDetector(loiter_sec=1.0, loiter_radius_px=50.0)
    box = [480, 400, 520, 500]
    det.update(0.0, [box], [1])
    evs = det.update(5.0, [box], [1])
    assert [e for e in evs if e.eventType == EVENT_LOITERING]


def test_ROI_설정이_분석기_설정으로_전달된다():
    """to_block_dict 가 3종을 모두 넘겨야 화면 설정이 탐지에 닿는다."""
    from tot_dashboard.core import cameras as C

    class _Row:
        enabled = True
        continuous = False
        config = {}

    class _Roi:
        shapes = {"analysis_roi": _square(0, 0, 10, 10),
                  "intrusion_roi": _square(20, 20, 30, 30),
                  "loiter_roi": _square(40, 40, 50, 50)}

    class _Cam:
        id, name, dept = "CAM-X", "지점", "부서"
        lat, lng = 35.1, 129.0
        sido, sigungu = "busan", "부산진구"
        source_type, source_url, source_path, cctv_name = "hls", "http://x", "", ""

        def domain_row(self, d):
            return _Row() if d == "crowd" else None

        def roi_row(self, d):
            return _Roi() if d == "crowd" else None

    crowd = C.to_block_dict(_Cam())["crowd"]
    assert set(crowd) >= {"analysis_roi", "intrusion_roi", "loiter_roi"}


def test_비어_있는_ROI는_넘기지_않는다():
    """빈 목록을 넘기면 분석기가 「영역이 있는데 비었다」로 오해한다."""
    from tot_dashboard.core import cameras as C

    class _Row:
        enabled = True
        continuous = False
        config = {}

    class _Roi:
        shapes = {"analysis_roi": [], "intrusion_roi": _square(0, 0, 5, 5)}

    class _Cam:
        id, name, dept = "CAM-Y", "지점", "부서"
        lat, lng = 35.1, 129.0
        sido, sigungu = "busan", "부산진구"
        source_type, source_url, source_path, cctv_name = "hls", "http://x", "", ""

        def domain_row(self, d):
            return _Row() if d == "crowd" else None

        def roi_row(self, d):
            return _Roi() if d == "crowd" else None

    crowd = C.to_block_dict(_Cam())["crowd"]
    assert "analysis_roi" not in crowd
    assert "intrusion_roi" in crowd
