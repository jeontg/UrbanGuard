import numpy as np

from tot_dashboard.crowd.behavior_tracker import CrowdBehaviorTracker


def test_empty_frame_returns_empty_metrics():
    tracker = CrowdBehaviorTracker(fps=10)
    metrics, det = tracker.update([], [], t=0.0)
    assert metrics["n_tracks"] == 0
    assert metrics["surge"] == 1.0


def test_moving_boxes_produce_nonzero_speed_after_two_frames():
    tracker = CrowdBehaviorTracker(fps=10)
    boxes_t0 = [[10, 10, 30, 30], [100, 100, 120, 120]]
    scores = [0.9, 0.9]
    tracker.update(boxes_t0, scores, t=0.0)
    boxes_t1 = [[20, 10, 40, 30], [110, 100, 130, 120]]  # moved +10px in x
    metrics, det = tracker.update(boxes_t1, scores, t=0.1)
    assert metrics["n_tracks"] >= 1
    assert metrics["mean_speed"] > 0
