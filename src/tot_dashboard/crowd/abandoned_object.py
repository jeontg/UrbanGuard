# -*- coding: utf-8 -*-
"""위험물 투기(Abandoned Object) 탐지 — GPU 불필요, 배경차분(고전 컴퓨터
비전 기법)만 사용, 새 딥러닝 모델 없음.

2026-08-29, 「4대탐지기능 성능개선 로드맵」 5단계(GPU 없이 가능한 대체안).
``behavior_events.py``(배회·침입·쓰러짐)와 짝을 이루는 별도 모듈이다 —
저 셋은 **사람 추적 결과**를 재해석하지만, 이건 **화면에 새로 나타난
정지 물체**를 찾는 문제라 사람 추적과 성격이 다르다(사람이 아닌 것을
찾아야 하므로 사람 검출기 출력만으로는 안 되고, 프레임 자체를 봐야
한다).

## 원리

OpenCV의 MOG2 배경차분기(``cv2.createBackgroundSubtractorMOG2``)로
"원래 배경과 다른" 전경 마스크를 얻는다. 사람은 이미 다른 경로(검출기)로
잡고 있으므로 그 상자 영역을 전경에서 제외해 사람을 물체로 오인하지
않는다. 남은 전경 덩어리가 **일정 시간 같은 자리에 머무르면** "놓인
물체"로 본다 — 사람처럼 계속 움직이는 전경은 이 조건을 만족하지 못해
자연히 걸러진다.

## ⚠️ 한계 — 정직하게 밝혀 둔다

- **무엇을 버렸는지는 구분하지 못한다.** 가방·쓰레기·낙엽 더미·주차된
  자전거 전부 같은 신호("새로 나타나 오래 머무는 물체")로 잡힌다
- 조명 급변(그림자·자동차 헤드라이트)·카메라 흔들림·화면 떨림은 알려진
  오탐 원인이다(MOG2 자체의 한계)
- 여러 사람이 겹쳐 있다 흩어지는 자리에 남는 그림자·잔상도 오탐할 수
  있다
- 확정된 위험물이 아니라 **의심** 수준이라 이름을 좁게
  ``AbandonedObject``로 잡았다(``behavior_events.py``의 쓰러짐·
  ``traffic_weather``의 사고 의심과 같은 원칙). 자동 문자 발송 경로도
  없다(이 프로젝트의 크라우드 이벤트는 전부 대시보드 표시까지만 —
  ``behavior_events.py`` 모듈 docstring 참고)
- 검증 안 된 시범값이다. 실제 배포 전 현장에서 지속시간·면적 임계값을
  조정할 것을 권장한다
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from .behavior_events import BehaviorEvent

EVENT_ABANDONED_OBJECT = "AbandonedObject"

# 판정 상수 — 전부 자체 판단값이다(외부 기준 없음). 실측 검증 전 시범값.
MIN_BLOB_AREA_FRAC = 0.001   # 프레임 면적 대비 최소 크기(잡음 제거)
MAX_BLOB_AREA_FRAC = 0.15    # 이보다 크면 조명 변화 등으로 보고 제외
PERSIST_SEC = 15.0           # 이 시간 이상 같은 자리에 머물러야 확정
MATCH_DIST_PX = 40.0         # 프레임 간 같은 물체로 볼 중심점 거리
MISS_GRACE_SEC = 3.0         # 이 시간 동안 안 보여도 후보를 유지(깜빡임 대응)
PERSON_DILATE_PX = 15        # 사람 상자를 이만큼 넓혀 제외 영역으로 삼는다

# ⚠️ MOG2 학습률 — 실측 중 발견한 함정. ``apply()``를 학습률 인자 없이
# (자동 모드) 부르면, 프레임 수가 아직 적을 때는 학습률이 매우 높아
# **새로 나타난 정지 물체가 단 몇 프레임 만에 배경으로 흡수**된다(직접
# 재현: 초기 10프레임 내에서 2번째 프레임부터 완전히 사라짐). 그러면
# ``PERSIST_SEC``까지 버티지 못해 이 기능 자체가 항상 무반응이 된다.
# 그래서 ①처음 ``BG_WARMUP_FRAMES``장은 자동 학습률로 배경을 빠르게
# 세우고, ②그 뒤로는 아주 느린 고정 학습률만 쓴다 — 실제 배경 변화
# (낮->밤 조도 등)에는 여전히 서서히 따라가되, 새로 놓인 물체는
# ``PERSIST_SEC`` 동안 배경에 흡수되지 않고 전경으로 남는다.
#
# ``BG_WARMUP_FRAMES``는 **호출 횟수** 기준이지 초 단위가 아니다 —
# 실제 배포에서는 카메라 한 대를 ``CROWD_INTERVAL_SEC``(기본 5초)마다
# 한 번씩만 부르므로, 워밍업이 끝나기까지 대략 30×5초=150초(약 2.5분)가
# 걸린다. 그동안 화면에 있던 것은 전부 배경으로 흡수된다 — 배경차분
# 계열 알고리즘의 통상적인 기동 특성이다.
BG_WARMUP_FRAMES = 30
BG_LEARNING_RATE = 0.001


@dataclass
class _Candidate:
    id: int
    cx: float
    cy: float
    first_seen: float
    last_seen: float
    area: float
    bbox: tuple[float, float, float, float]
    reported: bool = False


class AbandonedObjectDetector:
    """배경차분으로 새로 나타나 오래 머무는 물체를 찾는다.

    ``update()``를 프레임마다 호출하면 새로 확정된 이벤트만 반환한다
    (``behavior_events.BehaviorEventDetector``와 같은 호출 방식).

    ⚠️ **사람 상자는 실제 검출기 결과여야 한다.** mock(합성) 모드처럼
    사람 위치가 실제 화면과 무관하면, 잘못된 자리를 제외해 실제 사람을
    물체로 오탐하거나 반대로 물체가 있는 자리를 사람으로 착각해 놓칠 수
    있다 — 호출부(``live_analyzer.py``)가 detector 모드에서만 이 클래스를
    쓰는 이유다.
    """

    def __init__(self, block_id: str | None = None, node_id: str | None = None,
                 persist_sec: float = PERSIST_SEC,
                 min_area_frac: float = MIN_BLOB_AREA_FRAC,
                 max_area_frac: float = MAX_BLOB_AREA_FRAC):
        import cv2
        self._cv2 = cv2
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=32, detectShadows=True)
        self.block_id = block_id
        self.node_id = node_id
        self.persist_sec = persist_sec
        self.min_area_frac = min_area_frac
        self.max_area_frac = max_area_frac
        self._candidates: list[_Candidate] = []
        self._id_seq = itertools.count(1)
        self._frame_count = 0

    def update(self, t_sec: float, frame_bgr, person_boxes=None) -> list[BehaviorEvent]:
        cv2 = self._cv2
        h, w = frame_bgr.shape[:2]

        self._frame_count += 1
        learning_rate = -1.0 if self._frame_count <= BG_WARMUP_FRAMES else BG_LEARNING_RATE
        fg = self._bg.apply(frame_bgr, learningRate=learning_rate)
        # MOG2는 그림자를 127로 따로 표시한다 — 그림자를 물체로 잡지
        # 않도록 확실한 전경(255)만 쓴다.
        fg = ((fg == 255).astype("uint8")) * 255

        if person_boxes is not None and len(person_boxes):
            for b in person_boxes:
                x1 = max(0, int(b[0]) - PERSON_DILATE_PX)
                y1 = max(0, int(b[1]) - PERSON_DILATE_PX)
                x2 = min(w, int(b[2]) + PERSON_DILATE_PX)
                y2 = min(h, int(b[3]) + PERSON_DILATE_PX)
                fg[y1:y2, x1:x2] = 0

        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_area = float(h * w)
        min_area = self.min_area_frac * frame_area
        max_area = self.max_area_frac * frame_area

        blobs = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area or area > max_area:
                continue
            m = cv2.moments(c)
            if m["m00"] == 0:
                continue
            cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
            x, y, bw, bh = cv2.boundingRect(c)
            blobs.append((cx, cy, area, (float(x), float(y), float(x + bw), float(y + bh))))

        matched: set[int] = set()
        events: list[BehaviorEvent] = []
        for cx, cy, area, bbox in blobs:
            best_i, best_d = None, MATCH_DIST_PX
            for i, cand in enumerate(self._candidates):
                if i in matched:
                    continue
                d = ((cand.cx - cx) ** 2 + (cand.cy - cy) ** 2) ** 0.5
                if d < best_d:
                    best_i, best_d = i, d
            if best_i is not None:
                cand = self._candidates[best_i]
                cand.cx, cand.cy = cx, cy
                cand.last_seen = t_sec
                cand.area = area
                cand.bbox = bbox
                matched.add(best_i)
                if not cand.reported and (t_sec - cand.first_seen) >= self.persist_sec:
                    held = t_sec - cand.first_seen
                    cand.reported = True
                    events.append(BehaviorEvent(
                        eventType=EVENT_ABANDONED_OBJECT, trackId=cand.id,
                        confidence=min(0.5 + held / (self.persist_sec * 4), 0.9),
                        evidenceText=f"신규 정지물체 {held:.0f}초 이상 지속",
                        occursIn=self.block_id, detectedBy=self.node_id,
                        position=(cx, cy), bbox=cand.bbox))
            else:
                self._candidates.append(_Candidate(
                    id=next(self._id_seq), cx=cx, cy=cy, first_seen=t_sec,
                    last_seen=t_sec, area=area, bbox=bbox))

        # 오래 안 보인 후보는 버린다(메모리 누수 방지 + 사라진 물체를
        # 계속 붙들지 않기 위해).
        self._candidates = [c for c in self._candidates
                            if t_sec - c.last_seen <= MISS_GRACE_SEC]
        return events

    def summary(self) -> dict[str, Any]:
        return {
            "active_candidates": len(self._candidates),
            "reported": sum(1 for c in self._candidates if c.reported),
        }
