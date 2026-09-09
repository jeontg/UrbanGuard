"""CrowdBehaviorTracker — SAM3 boxes -> ByteTrack -> speed-surge / dispersion /
divergence.

Ported from SAM's ``SAM3_install.py`` (Stage 2). Unlike the rest of the crowd
domain, this class needs only ``supervision`` (already a base dependency, not
gated behind the ``crowd-gpu`` extra) and no GPU/SAM3 model — it operates on
plain box/score arrays regardless of what produced them, so it is fully
testable on CPU. flood3's ``traffic_tracker.TrafficBehaviorTracker`` is this
same tracker retargeted for vehicles (see docs/integration_plan.md section 2)
— the two were kept as separate classes rather than merged because their
derived metrics (traffic queue/stall vs. crowd surge/dispersion/divergence)
and consuming domains differ enough that a shared base would mostly just be
the ByteTrack wiring.
"""
from __future__ import annotations

from collections import defaultdict, deque

import numpy as np


class CrowdBehaviorTracker:
    """SAM3 boxes -> ByteTrack tracking -> speed-surge/dispersion/divergence."""

    def __init__(self, fps=30, hist=10):
        import supervision as sv

        from ..common import tracking
        self.sv = sv
        # 추적기는 어댑터가 고른다 — ``supervision.ByteTrack`` 은 0.31.0 에서
        # 제거되고 후속은 ``trackers`` 패키지다(``common/tracking.py`` 참고).
        self.tracker = tracking.Tracker(fps=fps)
        self.tracks = defaultdict(lambda: deque(maxlen=hist))
        self.speed_baseline = deque(maxlen=60)

    @property
    def backend(self) -> str:
        """어느 추적기를 쓰고 있나. 결과가 달라지면 여기부터 본다."""
        return self.tracker.backend

    def update(self, boxes, scores, t):
        sv = self.sv
        if len(boxes) == 0:
            return self._empty(), sv.Detections.empty()
        det = sv.Detections(
            xyxy=np.asarray(boxes, np.float32),
            confidence=np.asarray(scores, np.float32),
            class_id=np.zeros(len(boxes), int),
        )
        det = self.tracker.update(det)
        cents = {}
        # 추적기가 없으면 tracker_id 가 비어 있다. 그때는 속도를 못 구하지만
        # 인원수·밀집도는 계속 나와야 한다.
        if getattr(det, "tracker_id", None) is None:
            return self._empty(), det
        from ..common import tracking
        for xyxy, tid in zip(det.xyxy, det.tracker_id):
            # 미확정 트랙(-1)은 속도 계산에서 뺀다. 넣으면 여러 사람이 한
            # 트랙으로 뭉쳐 다음 프레임에 좌표가 급점프한다.
            if not tracking.is_confirmed(tid):
                continue
            cx, cy = (xyxy[0] + xyxy[2]) / 2, (xyxy[1] + xyxy[3]) / 2
            self.tracks[tid].append((t, cx, cy))
            cents[tid] = (cx, cy)
        vels = []
        for tid, h in self.tracks.items():
            if len(h) >= 2:
                (t0, x0, y0), (t1, x1, y1) = h[-2], h[-1]
                dt = max(t1 - t0, 1e-3)
                vels.append(((x1 - x0) / dt, (y1 - y0) / dt))
        vels = np.array(vels, np.float32) if vels else np.zeros((0, 2), np.float32)
        return self._metrics(vels, cents), det

    def _metrics(self, vels, cents):
        if len(vels) == 0:
            return self._empty()
        speeds = np.linalg.norm(vels, axis=1)
        mean_speed = float(speeds.mean())
        self.speed_baseline.append(mean_speed)
        base = np.median(self.speed_baseline) if self.speed_baseline else mean_speed
        surge = float(mean_speed / base) if base > 1e-3 else 1.0
        units = vels / (speeds[:, None] + 1e-6)
        dispersion = float(1.0 - np.linalg.norm(units.mean(axis=0)))
        divergence = 0.0
        if cents:
            C = np.array(list(cents.values()), np.float32)
            crowd_c = C.mean(axis=0)
            n = min(len(vels), len(C))
            radial = C[:n] - crowd_c
            radial /= (np.linalg.norm(radial, axis=1, keepdims=True) + 1e-6)
            divergence = float((vels[:n] * radial).sum(axis=1).mean())
        return dict(n_tracks=len(vels), mean_speed=mean_speed,
                    surge=surge, dispersion=dispersion, divergence=divergence)

    @staticmethod
    def _empty():
        return dict(n_tracks=0, mean_speed=0.0, surge=1.0, dispersion=0.0, divergence=0.0)
