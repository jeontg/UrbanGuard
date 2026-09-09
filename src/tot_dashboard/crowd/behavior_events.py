"""배회(Loitering)·침입(Intrusion)·쓰러짐(Fall) 탐지 — GPU 불필요, 추적 ID +
ROI + 박스 형태만 사용.

사양의 탐지 7종 중 **GPU 없이 구현 가능한 3종**을 담당한다
(docs/crowd_behavior_detection_plan.md 2-2절, 2026-08-29 「4대탐지기능
성능개선 로드맵」 5단계로 쓰러짐 추가). 군중급증·패닉은 ``semantic_risk_
agent.py``에 이미 있고, **폭력징후·위험물투기 중 폭력징후만** 여전히
행동인식(시간에 따른 움직임 패턴 인식) 모델이 필요해 GPU 확보 후 착수
대상으로 남는다(위험물투기는 별도 모듈 ``abandoned_object.py`` 참고 —
배경차분 방식으로 GPU 없이 구현했다).

## 왜 GPU가 필요 없는가

- **배회**: ``CrowdBehaviorTracker``(ByteTrack)가 이미 사람마다 추적 ID를 부여
  하고 있다. ID별로 관측 시각을 누적하면 체류시간(``dwell_time``)이 나온다.
- **침입**: ``common/roi.py``의 다각형 판정(``point_in_polygons``)을 그대로
  재사용한다. 침수 도메인의 ROI 인프라가 이미 검증돼 있다.
- **쓰러짐**: 새 모델 없이 **검출 상자의 가로/세로 비율(종횡비)** 만 본다.
  서 있는 사람은 세로로 길쭉하고(종횡비가 작음), 쓰러지면 가로로 넓적해진다
  (종횡비가 급격히 커짐). 저가형 상용 낙상감지 제품들이 실제로 쓰는 방식과
  같은 원리다(자세추정·행동인식 모델 없이 경계상자 형태만으로 판단).

즉 새 모델 없이 **기존 출력의 재해석**만으로 구현된다.

## ⚠️ 쓰러짐 판정의 한계 — 정직하게 밝혀 둔다

이 방식은 **자세의 결과(상자 모양)만 보고 원인은 구분하지 못한다.** 다음
경우 전부 종횡비가 비슷하게 변해 **오탐할 수 있다**:

- 물건을 줍거나 신발끈을 묶으려고 숙이는 동작
- 벤치·바닥에 앉거나 눕는 정상 행동
- 다른 사람·구조물에 가려 상자가 일그러지는 경우(가림·오검출)

그래서 ①일정 시간(기본 3초) 이상 그 상태가 **지속**돼야 하고, ②추적이
끊기지 않고 이어지는 같은 사람이어야 하며, ③확정된 사고가 아니라 **의심**
수준으로만 이름 붙였다(``traffic_weather``의 "사고 의심"과 같은 원칙 —
`incident_events.py` 참고). 이 모듈의 이벤트(배회·침입·쓰러짐 전부)는
애초에 ``event_sync.record_crowd_snapshot()``이 대시보드 이벤트로만 올릴
뿐 **자동 문자 발송 경로 자체가 없다**(SMS는 ``runner.py``의 침수·교통
판정에만 연결돼 있다) — 그래서 쓰러짐도 별도 예외처리 없이 이 구조를
그대로 따른다. 실제 배포 전 현장 검증을 거쳐 지속시간·임계값을 조정할
것을 권장한다.

## 환경 조건 결합 (사양 ④의 규칙 반영)

사양이 제시한 규칙을 그대로 구현한다:

    IF (LoiteringTime > threshold) AND (NightTime = True) AND (CommercialZone = True)
    THEN SuspiciousRisk ↑

야간·상업지구 여부는 ``field_sensors.EnvironmentProvider``와 블록 설정에서 온다.
현장 장비가 없어도 mock provider로 동작하므로 지금 개발·검증이 가능하다.

## ⚠️ 개인정보 유의

배회 탐지는 **개인 단위 행동 추적**이라 기존 익명 집계와 성격이 다르다. 추적 ID는
프레임 간 임시 식별자일 뿐 개인 식별정보가 아니지만, 체류시간·재방문 기록을
**영속 저장하면 개인정보에 해당할 수 있다.** 이 모듈은 메모리에만 유지하고
영속화하지 않으며, 저장이 필요하면 별도 검토가 선행돼야 한다
(docs/crowd_behavior_detection_plan.md 5절).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..common.roi import point_in_polygons

# 사양의 탐지 대상 코드 (semantic_risk_agent.RISK_TAXONOMY와 별개 축)
EVENT_LOITERING = "Loitering"
EVENT_INTRUSION = "Intrusion"
EVENT_FALL = "Fall"

# 쓰러짐 판정 상수 — 전부 자체 판단값이다(외부 임상 기준 없음). 실측
# 검증 전 시범값으로 취급할 것.
FALL_RATIO_FLOOR = 0.9      # 이 미만이면 "서 있는" 형태로 보고 기준치(EMA)에 반영
FALL_RATIO_JUMP = 1.7       # 종횡비가 평상시(기준치) 대비 이만큼 커지면 낙상 후보
FALL_HOLD_SEC = 3.0         # 낙상 후보 상태가 이만큼 지속돼야 확정(굽힘·가림 배제)
FALL_BASELINE_ALPHA = 0.1   # 기준치 지수이동평균 갱신 비율


@dataclass
class BehaviorEvent:
    """사양 5절 온톨로지의 ``BehaviorEvent`` 클래스에 대응.

    속성명은 사양(eventType/confidence/evidenceText/occursIn/detectedBy)을 따랐다.
    """
    eventType: str
    trackId: int
    confidence: float
    evidenceText: str
    occursIn: str | None = None          # Block id
    detectedBy: str | None = None        # SurveillanceNode(카메라) id
    dwellTimeSec: float | None = None
    position: tuple[float, float] | None = None
    # ★ S-88 증거 팝업이 「이벤트가 발생한 부분」에 쓴다(2026-08-20).
    #   **지어내지 않는다** — 이 이벤트를 만든 바로 그 트랙의 실제 추적
    #   상자(xyxy)다. `position`(발밑 점 하나)보다 구체적이라 상자로도 쓴다.
    bbox: tuple[float, float, float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventType": self.eventType, "trackId": self.trackId,
            "confidence": round(self.confidence, 3),
            "evidenceText": self.evidenceText,
            "occursIn": self.occursIn, "detectedBy": self.detectedBy,
            "dwellTimeSec": (round(self.dwellTimeSec, 1)
                             if self.dwellTimeSec is not None else None),
            "position": ([round(v, 1) for v in self.position]
                         if self.position else None),
            "bbox": ([round(float(v), 1) for v in self.bbox]
                    if self.bbox is not None else None),
        }


@dataclass
class _Track:
    first_seen: float
    last_seen: float
    positions: list[tuple[float, float]] = field(default_factory=list)
    reported_loitering: bool = False
    # None = 아직 한 번도 판정 안 함(최초 관측). 최초 관측 시점에 이미 ROI 안에
    # 있는 사람은 '진입'이 아니므로 이벤트를 내지 않는다 -- 밖->안 전환만 침입이다.
    inside_roi: bool | None = None
    reported_intrusion: bool = False
    # 쓰러짐 판정용. baseline_ratio는 "서 있을 때" 종횡비(가로/세로)의
    # 지수이동평균 -- 카메라 거리·각도마다 다르므로 트랙별로 스스로
    # 기준을 잡는다(고정 임계값을 화면 전체에 걸지 않는다).
    baseline_ratio: float | None = None
    fall_since: float | None = None
    reported_fall: bool = False


class BehaviorEventDetector:
    """추적 결과 + ROI + 환경조건 -> BehaviorEvent 목록.

    ``update()``를 프레임마다 호출하면 **새로 발생한 이벤트만** 반환한다
    (같은 사람에 대해 매 프레임 중복 경보가 나가지 않도록 1회만 보고).

    Parameters
    ----------
    loiter_sec : 이 시간(초) 이상 머무르면 배회로 판정
    loiter_radius_px : 이 반경 안에서만 움직였을 때 '머물렀다'고 봄
                       (이동 중인 통행자를 배회로 오판하지 않기 위함)
    intrusion_roi : 침입 판정 대상 다각형 목록(common.roi 포맷)
    night_multiplier : 야간일 때 배회 신뢰도 가중(사양 ④ 규칙)
    commercial_zone : 상업지구 여부(사양 ④ 규칙)
    """

    def __init__(self, loiter_sec: float = 120.0, loiter_radius_px: float = 80.0,
                 intrusion_roi: list | None = None, night_multiplier: float = 1.3,
                 commercial_zone: bool = False, block_id: str | None = None,
                 node_id: str | None = None, track_ttl_sec: float = 30.0,
                 loiter_roi: list | None = None):
        self.loiter_sec = loiter_sec
        self.loiter_radius_px = loiter_radius_px
        self.intrusion_roi = intrusion_roi or []
        # 배회를 볼 구역. 비어 있으면 화면 전체에서 판정한다.
        # 지정하면 그 안에 서 있는 사람만 배회로 본다 — 정류장 대기줄처럼
        # 오래 서 있는 것이 정상인 곳을 빼내기 위한 장치다.
        self.loiter_roi = loiter_roi or []
        self.night_multiplier = night_multiplier
        self.commercial_zone = commercial_zone
        self.block_id = block_id
        self.node_id = node_id
        self.track_ttl_sec = track_ttl_sec
        self._tracks: dict[int, _Track] = {}

    # -- 내부 유틸 ---------------------------------------------------
    @staticmethod
    def _foot_point(box) -> tuple[float, float]:
        """박스 하단 중앙(발점). 사람이 서 있는 지면 위치에 가장 가깝다."""
        return ((float(box[0]) + float(box[2])) * 0.5, float(box[3]))

    @staticmethod
    def _spread(points: list[tuple[float, float]]) -> float:
        """관측 위치들의 최대 이동 반경(중심 기준)."""
        if len(points) < 2:
            return 0.0
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        return max(((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 for p in points)

    def _prune(self, t_sec: float) -> None:
        """오래 안 보인 추적은 제거(메모리 누수 방지 + 개인정보 최소 보관)."""
        stale = [tid for tid, tr in self._tracks.items()
                 if t_sec - tr.last_seen > self.track_ttl_sec]
        for tid in stale:
            del self._tracks[tid]

    # -- 메인 --------------------------------------------------------
    def update(self, t_sec: float, boxes, track_ids, env=None) -> list[BehaviorEvent]:
        """한 프레임 처리 후 **새로 발생한** 이벤트 목록 반환.

        ``boxes``/``track_ids``는 ``CrowdBehaviorTracker`` 출력 형식을 그대로 받는다.
        ``env``는 ``field_sensors.EnvironmentState``(없으면 야간 가중 미적용).
        """
        events: list[BehaviorEvent] = []
        is_night = bool(getattr(env, "is_night", False))

        for box, tid in zip(boxes, track_ids):
            tid = int(tid)
            pt = self._foot_point(box)
            tr = self._tracks.get(tid)
            if tr is None:
                tr = _Track(first_seen=t_sec, last_seen=t_sec)
                self._tracks[tid] = tr
            tr.last_seen = t_sec
            tr.positions.append(pt)
            if len(tr.positions) > 200:          # 메모리 상한
                tr.positions = tr.positions[-200:]

            # ── 배회 판정 ──
            # 감시 구역을 지정했으면 그 안에 있는 사람만 본다.
            in_loiter_zone = (not self.loiter_roi
                              or point_in_polygons(pt, self.loiter_roi))
            dwell = tr.last_seen - tr.first_seen
            if (in_loiter_zone and not tr.reported_loitering
                    and dwell >= self.loiter_sec
                    and self._spread(tr.positions) <= self.loiter_radius_px):
                conf = min(dwell / (self.loiter_sec * 2), 1.0)
                ev_parts = [f"체류 {dwell:.0f}초"]
                if is_night:
                    conf = min(conf * self.night_multiplier, 1.0)
                    ev_parts.append("야간")
                if self.commercial_zone:
                    conf = min(conf * 1.15, 1.0)
                    ev_parts.append("상업지구")
                tr.reported_loitering = True
                events.append(BehaviorEvent(
                    eventType=EVENT_LOITERING, trackId=tid, confidence=conf,
                    evidenceText=" · ".join(ev_parts),
                    occursIn=self.block_id, detectedBy=self.node_id,
                    dwellTimeSec=dwell, position=pt,
                    bbox=tuple(float(v) for v in box)))

            # ── 침입 판정 (밖 -> 안 전환 순간 1회) ──
            if self.intrusion_roi:
                inside = point_in_polygons(pt, self.intrusion_roi)
                # tr.inside_roi is None -> 최초 관측. 이미 안에 있어도 '진입'이
                # 아니므로 이벤트 없이 상태만 기록한다.
                was_outside = tr.inside_roi is False
                if inside and was_outside and not tr.reported_intrusion:
                    tr.reported_intrusion = True
                    ev = ["통제구역 진입"]
                    if is_night:
                        ev.append("야간")
                    events.append(BehaviorEvent(
                        eventType=EVENT_INTRUSION, trackId=tid,
                        confidence=0.9 if is_night else 0.8,
                        evidenceText=" · ".join(ev),
                        occursIn=self.block_id, detectedBy=self.node_id,
                        position=pt, bbox=tuple(float(v) for v in box)))
                tr.inside_roi = inside

            # ── 쓰러짐 판정 (종횡비 급변 + 지속) ──
            # 위 클래스 docstring의 한계·안전장치를 그대로 따른다.
            if not tr.reported_fall:
                w = float(box[2]) - float(box[0])
                h = float(box[3]) - float(box[1])
                ratio = (w / h) if h > 1e-3 else 0.0

                if ratio < FALL_RATIO_FLOOR:
                    # "서 있는" 형태로 보이는 관측 -- 트랙 스스로의 기준치를 갱신
                    tr.baseline_ratio = (ratio if tr.baseline_ratio is None else
                                        tr.baseline_ratio * (1 - FALL_BASELINE_ALPHA)
                                        + ratio * FALL_BASELINE_ALPHA)
                    tr.fall_since = None
                elif (tr.baseline_ratio is not None
                      and ratio >= tr.baseline_ratio * FALL_RATIO_JUMP):
                    if tr.fall_since is None:
                        tr.fall_since = t_sec
                    elif t_sec - tr.fall_since >= FALL_HOLD_SEC:
                        held = t_sec - tr.fall_since
                        tr.reported_fall = True
                        conf = min(0.5 + held / (FALL_HOLD_SEC * 4), 0.9)
                        ev_parts = [f"종횡비 급변 {held:.0f}초 이상 지속"]
                        if is_night:
                            conf = min(conf * self.night_multiplier, 0.95)
                            ev_parts.append("야간")
                        events.append(BehaviorEvent(
                            eventType=EVENT_FALL, trackId=tid, confidence=conf,
                            evidenceText=" · ".join(ev_parts),
                            occursIn=self.block_id, detectedBy=self.node_id,
                            position=pt, bbox=tuple(float(v) for v in box)))
                else:
                    # 종횡비가 애매한 중간 상태 -- 지속 타이머를 리셋해 순간적인
                    # 굽힘·가림이 누적되지 않게 한다(오탐 방지).
                    tr.fall_since = None

        self._prune(t_sec)
        return events

    def summary(self) -> dict[str, Any]:
        """현재 추적 상태 요약(대시보드 표시용)."""
        return {
            "active_tracks": len(self._tracks),
            "loitering_reported": sum(1 for t in self._tracks.values()
                                      if t.reported_loitering),
            "intrusion_reported": sum(1 for t in self._tracks.values()
                                      if t.reported_intrusion),
            "fall_reported": sum(1 for t in self._tracks.values()
                                 if t.reported_fall),
        }
