"""Mock 도로 노면 손상 탐지 데이터 생성기.

Phase 1(UI 뼈대) 전용 자리표시자다 — 실제 AI 탐지 모델(road/defect_detection.py,
아직 미구현)이 준비되기 전까지 대시보드 화면 구성을 검증하기 위해서만 쓴다.
등급 이름/구간은 docs/road_surface_management_plan.md 3-2절의 예시안을 그대로
따르며, 임계값은 확정된 것이 아니다.

실제 모델이 준비되면 이 모듈은 폐기하고 road/defect_detection.py +
road/metrics_core.py 의 실측 결과로 교체한다. 모든 응답에는 ``mock: True`` 를
포함해, 프론트엔드가 실측 데이터와 혼동하지 않도록 한다.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass

GRADE_NAME = {1: "정상", 2: "관찰", 3: "보수 필요", 4: "긴급 보수"}
GRADE_COLOR = {1: "#3fb950", 2: "#79c0ff", 3: "#d4a017", 4: "#e5484d"}
DEFECT_TYPES = ["포트홀", "균열"]


@dataclass
class MockDefect:
    id: str
    type: str
    grade: int
    grade_label: str
    confidence: float
    x: float  # 0..1, 점검구간 내 상대 위치 (mock — 실제 ROI 좌표 아님)
    y: float
    detected_minutes_ago: int


def _rng_for(block_id: str) -> random.Random:
    # 블록 ID로 시드를 고정해, 새로고침해도 같은 블록은 같은 mock 결과를 보여준다
    # (실제 탐지처럼 매번 값이 바뀌면 "실측처럼" 보여 혼동을 줄 수 있어 피한다).
    return random.Random(block_id)


def mock_defects_for_block(block_id: str) -> list[MockDefect]:
    rng = _rng_for(block_id)
    n = rng.randint(0, 4)
    defects = []
    for i in range(n):
        grade = rng.choices([2, 3, 4], weights=[0.5, 0.35, 0.15])[0]
        defects.append(MockDefect(
            id=f"{block_id}-D{i + 1}",
            type=rng.choice(DEFECT_TYPES),
            grade=grade,
            grade_label=GRADE_NAME[grade],
            confidence=round(rng.uniform(0.55, 0.95), 2),
            x=round(rng.uniform(0.1, 0.9), 2),
            y=round(rng.uniform(0.1, 0.9), 2),
            detected_minutes_ago=rng.randint(1, 180),
        ))
    return defects


def block_summary(block_id: str, name: str) -> dict:
    defects = mock_defects_for_block(block_id)
    grade = max((d.grade for d in defects), default=1)
    return {
        "block_id": block_id,
        "name": name,
        "grade": grade,
        "grade_label": GRADE_NAME[grade],
        "defect_count": len(defects),
    }


def block_detail(block_id: str, name: str) -> dict:
    defects = mock_defects_for_block(block_id)
    grade = max((d.grade for d in defects), default=1)
    return {
        "block_id": block_id,
        "name": name,
        "grade": grade,
        "grade_label": GRADE_NAME[grade],
        "defects": [asdict(d) for d in defects],
    }
