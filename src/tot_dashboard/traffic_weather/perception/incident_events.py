"""교통 돌발상황 탐지 — 보행자·역주행·사고 의심(2026-08-26 신설).

`docs/202608260842/traffic_risk_types_vs_professional_solutions.md`에서
확인한 격차(전문 AID 솔루션 대비 사고·역주행·보행자 미보유)를 메운다.

## 왜 새 모델이 없어도 되는가

- **보행자**: YOLO가 이미 COCO ``person`` 클래스를 검출 중이고
  (``detection_source.PERSON_CLASSES``), ``perception.py``가 이미 분리해
  ``PerceptionState.persons``에 담고 있다 — 지금까지 침수 도메인 신호로만
  쓰였을 뿐이다. 배선만 하면 된다.
- **역주행**(추후 확장): 차량 궤적(``TrafficBehaviorTracker.tracks``)이
  이미 저장 중이다. "정상 진행방향" 기준값만 있으면 방향 비교는 로직만으로
  된다.
- **사고 의심**(추후 확장): 정지차량이 한 지점에 뭉치는 것을 대리지표로
  본다 — 사고 라벨 데이터가 없어 「사고 의심」이라는 이름부터 확정을
  피한다.

``crowd/behavior_events.py``(배회·침입 탐지)와 같은 설계다 — 추적 결과
+ ROI 판정만으로 새 모델 없이 돌발상황을 만든다.

## 왜 폴링 주기(20초)에 사건을 붙잡아 둬야 하는가

``event_sync.py``는 ``RiskStore`` 스냅샷을 20초마다 읽는다. 돌발상황은
트랙 단위로 **한 틱(0.2초)만 발생**하는 순간 사건이라, 스냅샷에 그대로
싣기만 하면 폴러가 거의 항상 놓친다. 그래서 ``active(t)``가 사건을
``incident_hold_sec``(기본 60초 — 폴링 주기의 3배) 동안 계속 "지금
활성"으로 돌려준다.

탐지 스레드(최대 5fps × 카메라 수) 안에서 곧바로 DB에 쓰지 않는 이유도
같다 — ``crowd``의 ``record_crowd_snapshot()``처럼 분석 쪽에서 직접
DB를 쓰는 방식은 crowd의 5초 주기 관측에는 맞지만, 교통 블록 루프처럼
훨씬 빠른 루프에 DB 지연을 얹으면 탐지 자체가 늦어진다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ...common.roi import point_in_polygons, scale_polygons
from ...models import RiskLevel


@dataclass(frozen=True)
class IncidentDef:
    code: str
    label: str
    hazard_type_code: str
    level: RiskLevel


# ⚠️ 등급은 최소 「주의」 이상이어야 한다 — `core/events.py`의
#   `EVENT_THRESHOLD = "주의"` 라 「관심」은 이벤트가 되지 않는다
#   (crowd 밀집도 이벤트가 겪은 것과 같은 함정).
INCIDENT_CATALOG: dict[str, IncidentDef] = {
    "TIN_PEDESTRIAN": IncidentDef(
        "TIN_PEDESTRIAN", "보행자 도로 진입", "traffic_pedestrian",
        RiskLevel.caution),
    "TIN_WRONGWAY": IncidentDef(
        "TIN_WRONGWAY", "역주행 의심", "traffic_wrongway",
        RiskLevel.alert),
    # ⚠️ "사고"가 아니라 "사고 의심"이다 — 사고 라벨로 학습한 모델이
    #   없고, 정지차량이 한 지점에 뭉치는 것을 **대리지표**로만 본다
    #   (§_update_accident 참고). 이름에서부터 확정을 피한다.
    "TIN_ACCIDENT": IncidentDef(
        "TIN_ACCIDENT", "사고 의심", "traffic_accident",
        RiskLevel.alert),
}

# ⚠️ [자체] — 「반대 방향」의 문턱. 헤딩과 화살표 방향의 각도차가 135°를
#   넘으면(즉 반대 방향에서 45° 이내) 역주행 후보로 본다. 90°(직교, 차로
#   변경 중)까지 잡으면 오탐이 너무 잦고, 180°(정확히 반대)만 잡으면
#   실제로 그 각도로 정확히 향하는 차가 거의 없어 놓친다. 부산 실측으로
#   정한 값이 아니므로 현장 확인 후 조정 대상이다.
WRONGWAY_ANGLE_DEG = 135.0
_WRONGWAY_COS_THRESHOLD = math.cos(math.radians(WRONGWAY_ANGLE_DEG))  # ≈ -0.7071


@dataclass
class TrafficIncident:
    """탐지된 돌발상황 1건. ``crowd.behavior_events.BehaviorEvent``와 같은
    골격 — 이벤트로 옮길 때 필요한 필드만 담는다."""
    code: str
    label: str
    level: RiskLevel
    confidence: float
    evidence_text: str
    hazard_type_code: str
    track_id: int | None = None
    position: tuple[float, float] | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "label": self.label,
            "level": self.level.value,
            "confidence": round(self.confidence, 3),
            "evidence_text": self.evidence_text,
            "hazard_type_code": self.hazard_type_code,
            "track_id": self.track_id,
            "position": ([round(v, 1) for v in self.position]
                        if self.position else None),
            "first_seen": round(self.first_seen, 2),
            "last_seen": round(self.last_seen, 2),
        }


@dataclass
class _PedestrianWatch:
    """보행자는 추적 ID가 없다(``perception.py``의 ``persons``는 프레임당
    발밑점만 준다) — 그래서 개인별이 아니라 **장면 단위**로 "도로 ROI 안에
    사람이 있는 연속 시간"을 잰다."""
    first_seen_in_roi: float | None = None  # 연속 관측 시작 시각. 끊기면 None.
    reported: bool = False                   # 이번 연속 관측에서 이미 보고했는가.


@dataclass
class _VehicleTrack:
    """역주행·사고 의심 판정용 차량 궤적 이력. 트래커
    (``TrafficBehaviorTracker``)는 건드리지 않는다 —
    ``crowd.behavior_events._Track``와 같은 방식으로 탐지기가 자기
    이력을 따로 갖는다."""
    positions: list[tuple[float, float, float]] = field(default_factory=list)  # (t, x, y)
    wrongway_since: float | None = None  # 반대 방향으로 보이기 시작한 연속 시각
    reported: bool = False               # 역주행 — 트랙당 1회만 발생
    stalled_since: float | None = None   # 정지로 보이기 시작한 연속 시각
    reported_accident: bool = False      # 이 트랙이 속한 군집을 이미 보고했는가


class TrafficIncidentDetector:
    """추적 결과 + ROI → 돌발상황. 트래커는 건드리지 않는다 — 자기 상태만
    갖는다(``crowd.behavior_events.BehaviorEventDetector``와 같은 방식)."""

    def __init__(self, *, pedestrian_roi: list | None = None,
                 pedestrian_exempt_roi: list | None = None,
                 roi_frame_wh: tuple[int | None, int | None] | None = None,
                 pedestrian_on_road_sec: float = 3.0,
                 incident_hold_sec: float = 60.0,
                 block_id: str | None = None,
                 flow_arrows: list | None = None,
                 heading_window_sec: float = 1.0,
                 heading_min_disp_px: float = 8.0,
                 wrongway_max_arrow_dist_px: float = 150.0,
                 wrongway_min_sec: float = 2.0,
                 accident_stall_sec: float = 12.0,
                 accident_cluster_px: float = 180.0,
                 accident_min_cluster: int = 2):
        # 새 ROI를 강제하지 않는다 — congestion_roi(정체 감시 구역)를
        # 그대로 재사용한다. 39대를 다시 그리게 하면 도입 비용이 크다.
        self.pedestrian_roi = pedestrian_roi or []
        self.pedestrian_exempt_roi = pedestrian_exempt_roi or []
        self.roi_frame_wh = roi_frame_wh or (None, None)
        self._roi_scaled: list | None = None
        self._exempt_scaled: list | None = None
        self.pedestrian_on_road_sec = pedestrian_on_road_sec
        self.incident_hold_sec = incident_hold_sec
        self.block_id = block_id
        self._ped_watch = _PedestrianWatch()
        # (만료 시각, 사건) 목록 — event_sync 폴링이 놓치지 않게 붙잡아 둔다.
        self._held: list[tuple[float, TrafficIncident]] = []

        # ★ 2026-08-26 — 역주행(Phase 4). ``flow_arrows``는 관리자가 그린
        #   "정상 방향" 화살표(``core/cameras.py`` ROI_SHAPES[TRAFFIC])다.
        #   **화살표가 없으면 판정 자체를 하지 않는다** — 기준이 없는데
        #   억지로 판정하면 조직적 오탐이 된다(계획 문서 §4-A 원칙과 동일).
        self.flow_arrows = flow_arrows or []
        self._arrows_scaled: list | None = None
        self.heading_window_sec = heading_window_sec
        # 정지 차량은 위치가 픽셀 단위로 미세하게 흔들려도 헤딩이 크게
        # 튄다(잡음) — 최소 변위 미만이면 방향을 재지 않는다.
        self.heading_min_disp_px = heading_min_disp_px
        self.wrongway_max_arrow_dist_px = wrongway_max_arrow_dist_px
        self.wrongway_min_sec = wrongway_min_sec
        self._vehicle_tracks: dict[int, _VehicleTrack] = {}

        # ★ 2026-08-26 — 사고 의심(Phase 5). [자체] 문턱 — 부산 실측으로
        #   정한 값이 아니다. 사고 라벨 데이터가 없어 원리적으로 적중률을
        #   검증할 수 없다(계획 문서 §13-5).
        self.accident_stall_sec = accident_stall_sec
        self.accident_cluster_px = accident_cluster_px
        self.accident_min_cluster = accident_min_cluster

    def _scaled(self, roi: list, cache_attr: str, frame_wh) -> list:
        """정체 감시 구역과 같은 방식으로 해상도를 보정한다
        (``TrafficBehaviorTracker._roi_polygons`` 와 동일 패턴)."""
        if not roi:
            return []
        cached = getattr(self, cache_attr)
        if cached is None and frame_wh:
            cached = scale_polygons(roi, self.roi_frame_wh, frame_wh)
            setattr(self, cache_attr, cached)
        return cached or roi

    def update(self, t: float, vehicles, persons: list[tuple[float, float]],
              frame_wh: tuple[int, int] | None = None, *,
              is_blocked: bool = False) -> None:
        """한 틱 처리. 새로 발생한 사건은 내부에 붙잡아 두고,
        ``active(t)``로 꺼내 쓴다(반환값 없음 — crowd와 인터페이스가
        다른 이유는 hold 창이 필요하기 때문).

        :param is_blocked: ``metrics.state == TrafficState.blocked`` —
            도로 전체가 정지 상태인가. 사고 의심 판정이 정체와 사고를
            구분하는 데 쓴다(``_update_accident`` 참고).
        """
        self._update_pedestrian(t, persons, frame_wh)
        self._update_wrongway(t, vehicles, frame_wh)
        self._update_accident(t, vehicles, is_blocked)

    def _update_pedestrian(self, t: float, persons, frame_wh) -> None:
        roi = self._scaled(self.pedestrian_roi, "_roi_scaled", frame_wh)
        exempt = self._scaled(self.pedestrian_exempt_roi, "_exempt_scaled", frame_wh)

        in_road_pt: tuple[float, float] | None = None
        for pt in persons:
            if roi and not point_in_polygons(pt, roi):
                continue
            if exempt and point_in_polygons(pt, exempt):
                continue
            in_road_pt = pt
            break  # 장면 단위 판정이라 한 명만 있어도 충분하다

        w = self._ped_watch
        if in_road_pt is None:
            # 도로 ROI에서 완전히 벗어났다 — 연속 관측이 끊겼으니 다음
            # 진입은 새 사건으로 볼 수 있게 초기화한다.
            w.first_seen_in_roi = None
            w.reported = False
            return

        if w.first_seen_in_roi is None:
            w.first_seen_in_roi = t
        dwell = t - w.first_seen_in_roi
        if dwell < self.pedestrian_on_road_sec or w.reported:
            return

        w.reported = True
        confidence = min(dwell / (self.pedestrian_on_road_sec * 2), 1.0)
        inc_def = INCIDENT_CATALOG["TIN_PEDESTRIAN"]
        incident = TrafficIncident(
            code=inc_def.code, label=inc_def.label, level=inc_def.level,
            confidence=confidence,
            evidence_text=f"도로 ROI 안 보행자 연속 관측 {dwell:.0f}초",
            hazard_type_code=inc_def.hazard_type_code,
            position=in_road_pt, first_seen=w.first_seen_in_roi, last_seen=t)
        self._held.append((t + self.incident_hold_sec, incident))

    def _update_wrongway(self, t: float, vehicles, frame_wh) -> None:
        """차량 헤딩을 가장 가까운 통행 방향 화살표와 비교한다.

        1. 최근 ``heading_window_sec`` 안의 변위로 헤딩을 잰다. 변위가
           ``heading_min_disp_px`` 미만이면(정지 차량) 방향을 재지 않는다.
        2. 가장 가까운 화살표에 배정한다. ``wrongway_max_arrow_dist_px``를
           넘으면(그 화살표의 관할 밖) 판정하지 않는다.
        3. 화살표 방향과 135°(:data:`WRONGWAY_ANGLE_DEG`) 넘게 어긋난
           상태가 ``wrongway_min_sec`` 이상 지속되면 트랙당 **1회만** 발생.
        """
        arrows = self._scaled(self.flow_arrows, "_arrows_scaled", frame_wh)
        if not arrows:
            # ★ 화살표가 없으면 "정상 방향" 기준이 없다는 뜻이다. 억지로
            #   판정하면 화살표를 안 그린 카메라 전부에서 오탐이 쏟아진다.
            return

        seen: set[int] = set()
        for v in vehicles:
            tid = v.track_id
            seen.add(tid)
            bbox = v.bbox
            # 판정 기준점은 다른 판정과 같은 bbox 하단 중심.
            x, y = (bbox[0] + bbox[2]) / 2.0, float(bbox[3])

            tr = self._vehicle_tracks.setdefault(tid, _VehicleTrack())
            tr.positions.append((t, x, y))
            if len(tr.positions) > 50:  # 메모리 상한
                tr.positions = tr.positions[-50:]
            if tr.reported:
                continue

            recent = [p for p in tr.positions if t - p[0] <= self.heading_window_sec]
            if len(recent) < 2:
                continue
            t0, x0, y0 = recent[0]
            t1, x1, y1 = recent[-1]
            if t1 <= t0:
                continue
            disp = math.hypot(x1 - x0, y1 - y0)
            if disp < self.heading_min_disp_px:
                tr.wrongway_since = None  # 정지 잡음 — 판정을 이어가지 않는다
                continue
            heading = math.atan2(y1 - y0, x1 - x0)

            arrow, arrow_dist = self._nearest_arrow(x1, y1, arrows)
            if arrow is None or arrow_dist > self.wrongway_max_arrow_dist_px:
                # 배정할 화살표가 없거나 너무 멀다(그 화살표의 관할 밖) —
                # 근거 없이 판정하지 않는다.
                tr.wrongway_since = None
                continue
            (ax1, ay1), (ax2, ay2) = arrow
            arrow_heading = math.atan2(ay2 - ay1, ax2 - ax1)
            cos_theta = math.cos(heading - arrow_heading)

            if cos_theta >= _WRONGWAY_COS_THRESHOLD:
                tr.wrongway_since = None  # 정상 방향(또는 애매한 차로변경) — 리셋
                continue
            if tr.wrongway_since is None:
                tr.wrongway_since = t
            dwell = t - tr.wrongway_since
            if dwell < self.wrongway_min_sec:
                continue

            tr.reported = True
            angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, cos_theta))))
            inc_def = INCIDENT_CATALOG["TIN_WRONGWAY"]
            incident = TrafficIncident(
                code=inc_def.code, label=inc_def.label, level=inc_def.level,
                confidence=min(dwell / (self.wrongway_min_sec * 2), 1.0),
                evidence_text=(f"정상 방향과 {angle_deg:.0f}° 어긋난 진행 "
                              f"{dwell:.1f}초 지속"),
                hazard_type_code=inc_def.hazard_type_code,
                track_id=tid, position=(x1, y1),
                first_seen=tr.wrongway_since, last_seen=t)
            self._held.append((t + self.incident_hold_sec, incident))

        # 오래 안 보인 트랙은 정리한다(메모리 누수 방지). 짧게 가려진
        # 트랙까지 지우면 이력이 날아가 판정이 매번 처음부터 시작되므로,
        # 넉넉한 유예(30초)를 둔다.
        stale = [tid for tid, tr in self._vehicle_tracks.items()
                if tid not in seen and tr.positions and t - tr.positions[-1][0] > 30.0]
        for tid in stale:
            del self._vehicle_tracks[tid]

    @staticmethod
    def _nearest_arrow(x: float, y: float,
                       arrows: list) -> tuple[list | None, float]:
        """차량 위치에서 가장 가까운 화살표(중점 기준)와 그 거리.

        거리를 함께 돌려주는 이유 — "관할 밖"(``wrongway_max_arrow_dist_px``
        초과) 판단은 호출부가 문턱값을 갖고 있으므로 여기서 미리 잘라내지
        않는다.
        """
        best, best_dist = None, float("inf")
        for arr in arrows:
            if not arr or len(arr) != 2:
                continue
            (ax1, ay1), (ax2, ay2) = arr
            mx, my = (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0
            d = math.hypot(x - mx, y - my)
            if d < best_dist:
                best_dist, best = d, arr
        return best, best_dist

    def _update_accident(self, t: float, vehicles, is_blocked: bool) -> None:
        """정지차량이 한 지점에 뭉치는 것을 사고 의심의 **대리지표**로
        본다.

        기존 ``TrafficMetrics.stalled``(``traffic_tracker.py``)로는 부족한
        이유 — 그 값은 ROI **전체 프레임 집계**라 「흩어진 정지 3대」와
        「한 지점에 뭉친 정지 3대」가 같은 숫자로 나온다. 후자만 사고
        신호다.

        1. 트랙별 연속 정지 시간(``stalled_since``)이 ``accident_stall_sec``
           이상인 트랙만 후보로 삼는다
        2. 후보를 발밑점 거리 ``accident_cluster_px`` 이내끼리 단일연결
           군집화한다
        3. 군집 크기가 ``accident_min_cluster`` 이상이면 그 군집에서
           아직 안 보고된 트랙이 하나라도 있을 때 **1건** 발생시키고,
           군집 전원을 "보고됨"으로 남긴다

        ⚠️ **정체와의 구분자** — 도로 전체가 정지 상태
        (``TrafficState.blocked``, ``is_blocked``)면 아예 판정하지 않는다.
        도로 전체가 서 있으면 그건 정체이지 사고가 아니다.

        ⚠️ **자동 문자 발송에서 제외된다** — 사용자 확정 사항(화면에만
        표시). 구조적으로도 이미 그렇다: SMS 발송은
        ``service/runner.py``의 ``_notify_decision``이 TWR_* 판정에만
        걸려 있고, 이 사건은 ``event_sync.py``의 별도 경로(이벤트 생성만)
        로만 흐른다 — 같은 경로를 새로 만들지 않는 한 저절로 제외된다.
        """
        if is_blocked:
            # 전면 정체 동안은 판단을 보류한다 — 정체가 풀렸는데도 여전히
            # 뭉쳐 있으면 그때 다시 후보로 잡힌다(stalled_since 는 여기서
            # 건드리지 않으므로 연속 정지 시간이 끊기지 않는다).
            return

        candidates: dict[int, tuple[float, float]] = {}
        for v in vehicles:
            tid = v.track_id
            tr = self._vehicle_tracks.setdefault(tid, _VehicleTrack())
            if v.stalled:
                if tr.stalled_since is None:
                    tr.stalled_since = t
            else:
                tr.stalled_since = None
                tr.reported_accident = False
            if (tr.stalled_since is not None
                    and t - tr.stalled_since >= self.accident_stall_sec):
                bbox = v.bbox
                candidates[tid] = ((bbox[0] + bbox[2]) / 2.0, float(bbox[3]))

        if len(candidates) < self.accident_min_cluster:
            return

        for group in self._cluster_by_distance(candidates, self.accident_cluster_px):
            if len(group) < self.accident_min_cluster:
                continue
            unreported = [tid for tid in group
                         if not self._vehicle_tracks[tid].reported_accident]
            if not unreported:
                continue  # 이 군집은 이미 보고했다 — 반복 보고하지 않는다
            for tid in group:
                self._vehicle_tracks[tid].reported_accident = True

            xs = [candidates[tid][0] for tid in group]
            ys = [candidates[tid][1] for tid in group]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            inc_def = INCIDENT_CATALOG["TIN_ACCIDENT"]
            incident = TrafficIncident(
                code=inc_def.code, label=inc_def.label, level=inc_def.level,
                confidence=min(len(group) / (self.accident_min_cluster * 2), 1.0),
                # ⚠️ 판정 근거를 있는 그대로 적는다 — 대리지표일 뿐인데
                #   "사고 발생"처럼 확정적으로 들리는 문구를 쓰면 안 된다.
                evidence_text=(f"정지 {len(group)}대가 {self.accident_cluster_px:.0f}px "
                              f"안에 {self.accident_stall_sec:.0f}초 이상 정지"),
                hazard_type_code=inc_def.hazard_type_code,
                position=(cx, cy), first_seen=t, last_seen=t)
            self._held.append((t + self.incident_hold_sec, incident))

    @staticmethod
    def _cluster_by_distance(points: dict[int, tuple[float, float]],
                             max_dist: float) -> list[set[int]]:
        """단일연결(single-linkage) 군집화 — 거리 ``max_dist`` 이내로
        연쇄적으로 이어지는 점들을 하나의 무리로 묶는다(union-find)."""
        parent = {tid: tid for tid in points}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        ids = list(points.keys())
        for i in range(len(ids)):
            ax, ay = points[ids[i]]
            for j in range(i + 1, len(ids)):
                bx, by = points[ids[j]]
                if math.hypot(ax - bx, ay - by) <= max_dist:
                    union(ids[i], ids[j])

        groups: dict[int, set[int]] = {}
        for tid in ids:
            groups.setdefault(find(tid), set()).add(tid)
        return list(groups.values())

    def active(self, t: float) -> list[TrafficIncident]:
        """지금 시각 기준으로 아직 hold 창 안인 사건들. 만료된 것은 여기서
        걷어낸다(메모리 누수 방지)."""
        self._held = [(exp, inc) for exp, inc in self._held if exp > t]
        return [inc for _exp, inc in self._held]
