import numpy as np

from tot_dashboard.crowd.behavior_tracker import CrowdBehaviorTracker


def test_empty_frame_returns_empty_metrics():
    tracker = CrowdBehaviorTracker(fps=10)
    metrics, det = tracker.update([], [], t=0.0)
    assert metrics["n_tracks"] == 0
    assert metrics["surge"] == 1.0


def test_moving_boxes_produce_nonzero_speed():
    """움직이면 속도가 나온다.

    ⚠️ **몇 프레임이 필요한지는 추적기 구현에 달렸다.**
    ``supervision.ByteTrack`` 은 첫 프레임부터 id 를 줘서 2프레임이면 됐지만,
    후속 ``trackers.ByteTrackTracker`` 는 **첫 프레임에 미확정(-1)** 을 주므로
    **3프레임**이 필요하다. 5fps 기준 0.4초 → 0.6초다.

    시험이 「2프레임」에 묶여 있으면 추적기를 바꿀 때마다 깨진다. 지키려는 것은
    **「움직이면 속도가 나온다」** 이지 프레임 수가 아니다.
    """
    tracker = CrowdBehaviorTracker(fps=10)
    scores = [0.9, 0.9]
    metrics = None
    for i in range(4):
        boxes = [[10 + i * 10, 10, 30 + i * 10, 30],
                 [100 + i * 10, 100, 120 + i * 10, 120]]   # 매 프레임 +10px
        metrics, _ = tracker.update(boxes, scores, t=i * 0.1)
    assert metrics["n_tracks"] >= 1
    assert metrics["mean_speed"] > 0
