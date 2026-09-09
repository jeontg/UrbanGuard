"""TrafficBehaviorTracker — tracks vehicle detections into per-frame TrafficMetrics.

Ported from flood3's ``perception/traffic_tracker.py``, itself a retargeting
of SAM's ``CrowdBehaviorTracker`` (crowd speed-surge -> vehicle speed-drop /
queueing / stalled vehicles). This SAM-to-flood3 lineage is a second, distinct
code-reuse path beyond the water-segmentation/risk-engine port from
underpath_flood_dashboard — see docs/integration_plan.md section 2.

Tracking backend priority:
1. Detections already carry a track_id -> use as-is (synthetic = ideal
   tracking, or an external tracker).
2. ``use_bytetrack=True`` -> ``supervision.ByteTrack`` (falls back to centroid
   if not installed).
3. Default -> built-in centroid matching (numpy, no dependency).

Speed is averaged over several track-history frames; speed_drop is
EMA-smoothed to damp per-frame flicker in the derived grade.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

import numpy as np

from ...common.roi import point_in_polygons, scale_polygons
from ...core.calibration import Calibration, traffic_speed_kmh
from ...models import Detection, TrafficMetrics, TrafficState, VehicleObject


class _CentroidTracker:
    """Lightweight nearest-centroid matching tracker (no dependencies)."""

    def __init__(self, max_dist: float = 90.0, max_lost: int = 5):
        self.max_dist = max_dist
        self.max_lost = max_lost
        self.next_id = 1
        self.objects: dict[int, tuple[float, float]] = {}
        self.lost: dict[int, int] = {}

    def update(self, centroids: list[tuple[float, float]]) -> list[int]:
        ids: list[int] = [-1] * len(centroids)
        if not self.objects:
            for i, c in enumerate(centroids):
                ids[i] = self._register(c)
            return ids
        obj_ids = list(self.objects.keys())
        if centroids:
            obj_pts = np.array([self.objects[i] for i in obj_ids], dtype=float)
            cur = np.array(centroids, dtype=float)
            dist = np.linalg.norm(obj_pts[:, None, :] - cur[None, :, :], axis=2)
            pairs = sorted((dist[m, n], m, n)
                           for m in range(len(obj_ids)) for n in range(len(centroids)))
            used_obj: set[int] = set()
            used_cur: set[int] = set()
            for d, m, n in pairs:
                if d > self.max_dist:
                    break
                oid = obj_ids[m]
                if oid in used_obj or n in used_cur:
                    continue
                self.objects[oid] = (cur[n][0], cur[n][1])
                self.lost[oid] = 0
                ids[n] = oid
                used_obj.add(oid)
                used_cur.add(n)
            for n, c in enumerate(centroids):
                if ids[n] == -1:
                    ids[n] = self._register(c)
            for oid in obj_ids:
                if oid not in used_obj:
                    self._mark_lost(oid)
        else:
            for oid in obj_ids:
                self._mark_lost(oid)
        return ids

    def _register(self, c: tuple[float, float]) -> int:
        i = self.next_id
        self.next_id += 1
        self.objects[i] = (c[0], c[1])
        self.lost[i] = 0
        return i

    def _mark_lost(self, oid: int) -> None:
        self.lost[oid] = self.lost.get(oid, 0) + 1
        if self.lost[oid] > self.max_lost:
            self.objects.pop(oid, None)
            self.lost.pop(oid, None)


def _center(bbox) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


class TrafficBehaviorTracker:
    def __init__(self, fps: float = 5.0, hist: int = 8,
                 stall_speed: float = 15.0, slow_speed: float = 60.0,
                 speed_window: int = 4, ema_alpha: float = 0.5,
                 baseline_hint: float | None = None,
                 use_bytetrack: bool = False,
                 congestion_roi: list | None = None,
                 roi_frame_wh: tuple[int | None, int | None] | None = None,
                 cal: Calibration | None = None):
        # ★ 2026-08-26 — cal(``core.calibration.Calibration`` | None).
        #   ``cal.ground`` 가 있으면 속도를 km/h 로도 낸다
        #   (``core.calibration.traffic_speed_kmh``). **px/s 판정 문턱
        #   (stall_speed·slow_speed)은 절대 이걸로 바꾸지 않는다** — 두 값
        #   다 여전히 px/s 기준이고, km/h 는 표시용으로만 옆에 추가한다.
        #   문턱을 km/h 로 바꾸면 지금까지의 정체 판정이 전부 어긋난다.
        self.cal = cal or Calibration()
        # ★ 2026-08-22 congestion_roi 추가 — S-81 에서 저장한 교통위험
        #   ``congestion_roi`` 가 그동안 **저장만 되고 판정에는 전혀 쓰이지
        #   않았다.** ``REQUIRED_SHAPE`` 가 이 도형을 필수로 강제해 관리자가
        #   반드시 그려야 했는데, 그려도 정체 판정은 화면 전체를 그대로 썼다
        #   (전수점검에서 발견).
        self.congestion_roi = congestion_roi or []
        self.roi_frame_wh = roi_frame_wh or (None, None)
        self._roi_scaled: list | None = None   # 프레임 크기 확정 후 1회 보정
        self.fps = fps
        self.hist = hist
        self.stall_speed = stall_speed  # px/s below this = stalled vehicle
        self.slow_speed = slow_speed  # px/s below this = queued
        self.speed_window = speed_window  # frames averaged for speed
        self.ema_alpha = ema_alpha
        self.tracks: dict[int, deque] = defaultdict(lambda: deque(maxlen=hist))
        self._age: dict[int, int] = {}  # per-track frames-tracked age
        self.last_objects: list[VehicleObject] = []  # most recent frame's vehicle objects
        self._speed_hist: deque = deque(maxlen=int(max(fps, 1) * 6))
        # free-flow baseline (running max of rolling median). A hint gives an
        # immediate accurate drop ratio (synthetic = known base_speed); real
        # YOLO sources have none -> learned from observation.
        self._baseline = float(baseline_hint) if baseline_hint else 0.0
        self._n_seen = 0
        self._drop_ema: float | None = None
        self._frame_area: float | None = None
        self.backend, self._bt = self._init_backend(use_bytetrack)
        self._centroid = _CentroidTracker()
        # ★ 2026-08-26 — 통행 방향 자동 생성(Phase 4-A, perception.flow_
        #   learning)이 쓸 변위 벡터를 모아 둔다. 트랙 "완료" 시점을 따로
        #   잡지 않고(이 트래커는 트랙을 명시적으로 안 지운다) sp_obj 계산에
        #   이미 쓰는 (x0,y0)→(x1,y1) 구간을 그대로 재사용한다 — 새 이력을
        #   또 안 만든다. 상한(5000)은 카메라 1대·수 시간 관측 기준 메모리
        #   보호용이며 학습 알고리즘과는 무관하다.
        self.flow_samples: deque = deque(maxlen=5000)
        self.last_frame_wh: tuple[int, int] | None = None

    def _init_backend(self, use_bytetrack: bool):
        """추적기를 고른다. 실패하면 무게중심 추적으로 내려간다.

        ``supervision.ByteTrack`` 은 0.31.0 에서 제거되므로 어댑터
        (``common/tracking.py``)를 거친다 — 후속 패키지가 있으면 그쪽을,
        없으면 폐기 예정 구현을 쓴다.
        """
        if use_bytetrack:
            try:
                from ...common import tracking
                t = tracking.Tracker(fps=self.fps)
                if t.available:
                    return "bytetrack", t
                print("[tracker] 추적기 없음 -> centroid fallback")
            except Exception as e:  # noqa: BLE001
                print(f"[tracker] tracking adapter unavailable -> centroid "
                      f"fallback: {str(e)[:80]}")
        return "centroid", None

    def _track(self, detections: list[Detection], t: float):
        """Returns list[(bbox, track_id)] — backend-agnostic."""
        if detections and all(d.track_id is not None for d in detections):
            return [(d.bbox, int(d.track_id)) for d in detections]
        if self.backend == "bytetrack":
            import supervision as sv
            if not detections:
                # 빈 프레임도 넣어야 사라진 트랙이 제때 정리된다.
                self._bt.update(sv.Detections.empty())
                return []
            det = sv.Detections(
                xyxy=np.array([d.bbox for d in detections], dtype=float),
                confidence=np.array([d.conf for d in detections], dtype=float),
                class_id=np.zeros(len(detections), dtype=int))
            det = self._bt.update(det)
            if getattr(det, "tracker_id", None) is None:
                # 추적이 안 되면 무게중심으로 내려간다 — id 없이 돌려주면
                # 부르는 쪽이 매 프레임 새 차량으로 센다.
                centroids = [_center(d.bbox) for d in detections]
                ids = self._centroid.update(centroids)
                return [(d.bbox, tid) for d, tid in zip(detections, ids)]
            # 미확정(-1)은 제외한다. 여러 차량이 한 id 로 뭉치면 대기열·정체
            # 지표가 통째로 어긋난다.
            # ⚠️ 점 개수를 틀리면 **패키지 밖으로 나가** ImportError 가 난다.
            #    여기는 tot_dashboard.traffic_weather.perception 이라
            #    tot_dashboard.common 은 `...` 세 개다. 네 개로 적혀 있어
            #    **침수 블록 루프가 매 틱마다 통째로 실패**하고 있었다
            #    (2026-08-20 전체 점검에서 54,515회 확인).
            from ...common import tracking
            return [(tuple(xyxy), int(tid))
                    for xyxy, tid in zip(det.xyxy, det.tracker_id)
                    if tracking.is_confirmed(tid)]
        centroids = [_center(d.bbox) for d in detections]
        ids = self._centroid.update(centroids)
        return [(d.bbox, tid) for d, tid in zip(detections, ids)]

    def update(self, detections: list[Detection], t: float,
               frame_wh: tuple[int, int] | None = None) -> TrafficMetrics:
        if frame_wh:
            self._frame_area = float(frame_wh[0] * frame_wh[1])
            # ★ 2026-08-26 — flow_samples(통행 방향 자동 생성용 변위)가
            #   어느 해상도 기준인지 기억해 둔다. ROI 화면은 "정지영상"
            #   해상도로 좌표를 다루는데, 실시간 탐지 프레임 해상도가 다를
            #   수 있다(이 파일이 겪어 온 반복된 함정과 같은 부류) — 호출부
            #   (routes_cameras.py 의 "자동 생성")가 이 값으로 보정한다.
            self.last_frame_wh = (int(frame_wh[0]), int(frame_wh[1]))
        tracked = self._track(detections, t)

        roi = self._roi_polygons(frame_wh)

        per = []  # (bbox, tid, speed, has_speed, speed_kmh, in_roi)
        speeds: list[float] = []
        speeds_kmh: list[float] = []
        for bbox, tid in tracked:
            cx, cy = _center(bbox)
            # ⚠️ **추적은 언제나 전체 프레임 대상이다.** ROI 밖이라고 트랙을
            #   끊으면 차가 ROI 를 드나들 때마다 id 가 바뀌어 속도가 0 이 된다.
            #   거르는 것은 아래 **집계 단계**뿐이다.
            self.tracks[tid].append((t, cx, cy))
            self._age[tid] = self._age.get(tid, 0) + 1
            h = self.tracks[tid]
            sp_obj, has = 0.0, len(h) >= 2
            sp_kmh: float | None = None
            if has:  # average speed over the recent speed_window frames
                k = min(self.speed_window, len(h))
                (t0, x0, y0), (t1, x1, y1) = h[-k], h[-1]
                dt = max(t1 - t0, 1e-3)
                sp_obj = math.hypot(x1 - x0, y1 - y0) / dt
                # ★ km/h 는 **표시용으로만** 곁들인다 — 위 sp_obj(px/s) 기반
                #   판정(stall_speed·slow_speed)에는 전혀 관여하지 않는다.
                #   dt 는 sp_obj 와 **같은 문턱(1e-3)으로 바닥을 둔다** — 안
                #   그러면 아주 짧은 dt 에서 sp_obj 는 안전하게 눌리는데
                #   km/h 만 비정상적으로 튀는 값을 낼 수 있다.
                if self.cal.ground is not None:
                    sp_kmh = traffic_speed_kmh(
                        self.cal, (x0, y0), (x1, y1), dt)
            # 판정 기준점은 침수 도메인과 같은 **bbox 하단 중심**이다
            # (노면과 맞닿는 지점이라 원근 왜곡에 가장 덜 흔들린다).
            in_roi = True if not roi else point_in_polygons(
                ((bbox[0] + bbox[2]) / 2.0, float(bbox[3])), roi)
            if has and in_roi:
                speeds.append(sp_obj)
                if sp_kmh is not None:
                    speeds_kmh.append(sp_kmh)
                # flow_learning.propose_arrows() 가 쓸 변위 벡터. sp_obj 와
                # 같은 (x0,y0)→(x1,y1) 구간을 재사용한다 — 정지 차량(변위
                # 작음)은 학습 쪽 min_disp_px 문턱에서 알아서 걸러진다.
                self.flow_samples.append((x0, y0, x1, y1))
            per.append((bbox, tid, sp_obj, has, sp_kmh, in_roi))

        sp = np.array(speeds, dtype=float) if speeds else np.zeros(0)
        mean_speed = float(sp.mean()) if sp.size else 0.0

        # free-flow baseline: running max of rolling median (early no-rain window is the reference).
        if sp.size:
            self._speed_hist.append(mean_speed)
            self._n_seen += 1
            if self._n_seen >= 3:
                self._baseline = max(self._baseline, float(np.median(self._speed_hist)))
        base = self._baseline if self._baseline > 1e-6 else mean_speed
        raw_drop = min(max(1.0 - mean_speed / base, 0.0), 1.0) if base > 1e-6 else 0.0
        self._drop_ema = raw_drop if self._drop_ema is None \
            else self.ema_alpha * raw_drop + (1 - self.ema_alpha) * self._drop_ema
        speed_drop = self._drop_ema

        stalled = int((sp < self.stall_speed).sum()) if sp.size else 0
        queue_len = int((sp < self.slow_speed).sum()) if sp.size else 0
        mean_speed_kmh = (round(float(np.mean(speeds_kmh)), 1)
                          if speeds_kmh else None)

        n_in_roi = sum(1 for *_x, in_roi in per if in_roi)
        density = 0.0
        if self._frame_area:
            area = sum(max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
                       for b, _tid, _sp, _has, _sp_kmh, in_roi in per if in_roi)
            # ⚠️ 분모는 ROI 넓이가 아니라 **프레임 넓이 그대로**다. 분모를
            #   ROI 로 바꾸면 같은 정체가 ROI 크기에 따라 다른 밀도로 나와,
            #   기존에 조정해 둔 판정 문턱이 전부 어긋난다(문턱 재조정은
            #   현장 캘리브레이션이 선행돼야 하는 별도 과제).
            density = min(area / self._frame_area, 1.0)

        # ── per-vehicle object attributes: speed + drop vs. free flow ──
        # ⚠️ ``last_objects`` 는 **거르지 않는다.** service/runner.py 의 침수
        #   판정이 이 목록으로 물-차량 접촉(vehicles_touching_water)을 본다 —
        #   교통 ROI 로 걸러 버리면 침수 판정이 회귀한다. 교통 ROI 필터는
        #   오직 위의 TrafficMetrics 집계에만 적용된다.
        objs = []
        for bbox, tid, sp_obj, has, sp_kmh, _in_roi in per:
            drop = min(max(1.0 - sp_obj / base, 0.0), 1.0) if (base > 1e-6 and has) else 0.0
            objs.append(VehicleObject(
                track_id=int(tid), bbox=tuple(float(v) for v in bbox),
                speed=round(sp_obj, 1),
                speed_kmh=round(sp_kmh, 1) if sp_kmh is not None else None,
                speed_drop=round(drop, 3),
                age=self._age[tid], stalled=bool(has and sp_obj < self.stall_speed)))
        self.last_objects = objs

        return TrafficMetrics(
            t_sec=round(t, 2), n_vehicles=n_in_roi,
            mean_speed=round(mean_speed, 2), mean_speed_kmh=mean_speed_kmh,
            speed_drop=round(speed_drop, 3),
            density=round(density, 3), queue_len=queue_len, stalled=stalled,
            state=self._state(speed_drop, stalled, mean_speed))

    def _roi_polygons(self, frame_wh: tuple[int, int] | None) -> list:
        """실제 프레임 좌표계로 옮긴 정체 감시 구역(1회만 계산해 캐시).

        ROI 는 정지영상 픽셀 좌표로 저장되는데 라이브 프레임 해상도가 다를
        수 있다(``common.roi.scale_polygons`` 참고). 크기를 모르면 보정을
        건너뛰고 원본을 그대로 쓴다 — 잘못 늘이는 것보다 낫다.
        """
        if not self.congestion_roi:
            return []
        if self._roi_scaled is None and frame_wh:
            self._roi_scaled = scale_polygons(
                self.congestion_roi, self.roi_frame_wh, frame_wh)
        return self._roi_scaled or self.congestion_roi

    def _state(self, drop: float, stalled: int, mean_speed: float) -> TrafficState:
        if mean_speed < self.stall_speed and stalled >= 2:
            return TrafficState.blocked
        if drop > 0.5:
            return TrafficState.congested
        if drop > 0.2:
            return TrafficState.slow
        return TrafficState.free
