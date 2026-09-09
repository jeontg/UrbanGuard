"""``congestion_roi`` 가 실제 정체 판정에 쓰이는가 (2026-08-22 전수점검).

## 왜 이 시험이 있나

S-81 에서 저장한 교통위험 ``congestion_roi`` 가 **저장만 되고 판정에는 전혀
쓰이지 않았다.** ``REQUIRED_SHAPE`` 가 이 도형을 필수로 강제해 관리자가 반드시
그려야 했는데, 그려도 정체 판정은 화면 전체를 그대로 썼다. 화면은 "설정하지
않으면 판정이 부정확합니다"라고 안내하고 있었으니 **사실과 다른 안내**였다.

지켜야 할 것.

* ROI 밖 차량은 **집계(대수·속도·정체)에서 빠진다**
* 그러나 **추적은 전체 프레임 대상으로 계속된다** — ROI 를 드나들 때마다
  트랙 id 가 바뀌면 속도가 0 이 된다
* ``last_objects`` 는 **거르지 않는다** — 침수 판정이 물-차량 접촉을 볼 때
  이 목록을 쓰므로, 여기서 걸러 내면 침수가 회귀한다
* ROI 미설정이면 **예전과 똑같이** 화면 전체 기준으로 판정한다
"""
from __future__ import annotations

from tot_dashboard.models import Detection
from tot_dashboard.traffic_weather.perception.traffic_tracker import (
    TrafficBehaviorTracker,
)

# 좌측 절반만 감시 구역으로 삼는다(프레임 400x200 기준).
LEFT_HALF = [[[0, 0], [200, 0], [200, 200], [0, 200]]]
FRAME_WH = (400, 200)


def _det(x1, y1, x2, y2, cls="car"):
    return Detection(cls_name=cls, confidence=0.9, bbox=(x1, y1, x2, y2))


def _feed(tracker, dets_per_tick, *, ticks=4, dt=0.2):
    """같은 위치의 검출을 여러 틱 먹여 트랙이 속도를 낼 만큼 쌓는다."""
    m = None
    for i in range(ticks):
        m = tracker.update(dets_per_tick(i), t=i * dt, frame_wh=FRAME_WH)
    return m


def test_ROI_밖_차량은_대수에서_빠진다():
    tracker = TrafficBehaviorTracker(congestion_roi=LEFT_HALF,
                                     roi_frame_wh=FRAME_WH)
    # 좌(ROI 안) 1대 + 우(ROI 밖) 2대
    m = _feed(tracker, lambda i: [_det(20, 100, 60, 150),
                                  _det(300, 100, 340, 150),
                                  _det(350, 100, 390, 150)])
    assert m.n_vehicles == 1, f"ROI 밖 차량까지 셌다: {m.n_vehicles}"


def test_ROI_미설정이면_전체_화면_기준_그대로():
    """★ 하위호환 — ROI 를 안 그린 카메라가 갑자기 0대로 보이면 안 된다."""
    tracker = TrafficBehaviorTracker()  # congestion_roi 없음
    m = _feed(tracker, lambda i: [_det(20, 100, 60, 150),
                                  _det(300, 100, 340, 150),
                                  _det(350, 100, 390, 150)])
    assert m.n_vehicles == 3


def test_last_objects는_거르지_않는다():
    """★ 침수 판정(vehicles_touching_water)이 이 목록을 그대로 쓴다."""
    tracker = TrafficBehaviorTracker(congestion_roi=LEFT_HALF,
                                     roi_frame_wh=FRAME_WH)
    _feed(tracker, lambda i: [_det(20, 100, 60, 150),
                              _det(300, 100, 340, 150)])
    assert len(tracker.last_objects) == 2, (
        "교통 ROI 필터가 last_objects 까지 걸렀다 — 침수 판정이 회귀한다")


def test_추적은_ROI_밖에서도_계속된다():
    """ROI 를 드나드는 차의 트랙이 끊기면 속도가 0 이 된다."""
    tracker = TrafficBehaviorTracker(congestion_roi=LEFT_HALF,
                                     roi_frame_wh=FRAME_WH)
    # 오른쪽(ROI 밖)에서 왼쪽(ROI 안)으로 이동
    def moving(i):
        x = 320 - i * 40
        return [_det(x, 100, x + 40, 150)]

    _feed(tracker, moving, ticks=6)
    # ROI 밖 구간에서도 트랙이 이어졌다면 히스토리가 6개 쌓여 있어야 한다.
    assert len(tracker.tracks) == 1, "ROI 를 드나들며 트랙 id 가 갈라졌다"
    (hist,) = tracker.tracks.values()
    assert len(hist) >= 5


def test_ROI_안_차량만_정지_판정에_들어간다():
    """ROI 밖에 정지 차량이 아무리 많아도 우리 구역 판정은 그대로여야 한다."""
    roi_only = TrafficBehaviorTracker(congestion_roi=LEFT_HALF,
                                      roi_frame_wh=FRAME_WH)
    m_roi = _feed(roi_only, lambda i: [_det(20, 100, 60, 150),      # ROI 안, 정지
                                       _det(300, 100, 340, 150),    # ROI 밖, 정지
                                       _det(350, 100, 390, 150)])   # ROI 밖, 정지
    assert m_roi.stalled <= 1, f"ROI 밖 정지차량까지 셌다: {m_roi.stalled}"


def test_해상도가_다르면_ROI_좌표를_보정한다():
    """ROI 를 그린 정지영상이 800x400 이고 실제 프레임이 400x200 이면
    좌표를 절반으로 줄여 읽어야 한다."""
    tracker = TrafficBehaviorTracker(
        congestion_roi=[[[0, 0], [400, 0], [400, 400], [0, 400]]],  # 800폭 기준 좌측 절반
        roi_frame_wh=(800, 400))
    # 실제 프레임(400x200)에서 x=20 은 보정 후에도 ROI 안이어야 한다.
    m = _feed(tracker, lambda i: [_det(20, 100, 60, 150),
                                  _det(300, 100, 340, 150)])
    assert m.n_vehicles == 1


# --- 통행 방향 자동 생성용 변위 수집 (Phase 4-A, 2026-08-26) ------------------


def test_ROI_안에서_이동하면_변위_벡터가_쌓인다():
    """flow_learning.propose_arrows() 가 쓸 원자재 — sp_obj 계산에 이미
    쓰는 구간을 재사용한다."""
    tracker = TrafficBehaviorTracker(congestion_roi=LEFT_HALF,
                                     roi_frame_wh=FRAME_WH)
    _feed(tracker, lambda i: [_det(20 + i * 20, 100, 60 + i * 20, 150)])
    assert len(tracker.flow_samples) > 0
    x0, _y0, x1, _y1 = tracker.flow_samples[-1]
    assert x1 > x0, "오른쪽으로 이동했는데 변위 벡터가 반대로 나왔다"


def test_ROI_밖_이동은_변위_벡터에_안_쌓인다():
    tracker = TrafficBehaviorTracker(congestion_roi=LEFT_HALF,
                                     roi_frame_wh=FRAME_WH)
    _feed(tracker, lambda i: [_det(300 + i * 5, 100, 340 + i * 5, 150)])  # ROI 밖
    assert len(tracker.flow_samples) == 0
