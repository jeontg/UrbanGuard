"""도로 노면 손상 보고서(마크다운) 생성.

``traffic_weather/report_generator.py``와 동일한 anti-hallucination 설계를
따른다 — 등급·손상목록·권고는 규칙 기반 **고정 필드**이며, 「종합 판단」
문단만 선택적으로 AI(Gemini)가 서술한다(``situation_agent.py`` 참고).

⚠ Phase 1/2(road/mock_data.py) 기준 — 손상 데이터 자체가 mock이므로, 보고서
최상단에 항상 mock 경고를 포함한다.
"""
from __future__ import annotations

from datetime import datetime

RULE_SRC = "규칙 서술"
AI_SRC = "AI 서술 · Gemini"

_FALLBACK_BY_GRADE = {
    1: "탐지된 노면 손상이 없습니다 — 특이사항 없음.",
    2: "경미한 손상이 관찰됩니다 — 정기 점검 시 확인을 권장합니다.",
    3: "보수가 필요한 손상이 감지되었습니다 — 현장 점검 및 보수 일정 수립이 필요합니다.",
    4: "긴급 보수가 필요한 손상이 감지되었습니다 — 조속한 현장 확인과 안전 조치를 권고합니다.",
}
_RECOMMENDATION_BY_GRADE = {
    1: "추가 조치 불필요, 정기 점검 유지",
    2: "다음 정기 점검 시 확인",
    3: "현장 점검 후 보수 일정 수립",
    4: "긴급 현장 점검 및 안전 조치(라바콘 설치 등) 우선 시행",
}


def build_road_briefing(detail: dict, narrative: str | None = None) -> str:
    grade = detail.get("grade", 1)
    grade_label = detail.get("grade_label", "정상")
    defects = detail.get("defects", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary = narrative.strip() if narrative else _FALLBACK_BY_GRADE.get(grade, "")
    narr_src = AI_SRC if narrative else RULE_SRC
    recommendation = _RECOMMENDATION_BY_GRADE.get(grade, "정기 점검 유지")

    if defects:
        rows = "\n".join(
            f"| {d['type']} | 등급 {d['grade']}({d['grade_label']}) | {d['confidence'] * 100:.0f}% | "
            f"({d['x'] * 100:.0f}%, {d['y'] * 100:.0f}%) | {d['detected_minutes_ago']}분 전 |"
            for d in defects
        )
        defects_table = (
            "| 유형 | 등급 | 신뢰도 | 점검구간 내 위치 | 탐지 시점 |\n"
            "|---|---|---|---|---|\n" + rows
        )
    else:
        defects_table = "탐지된 손상 없음"

    return f"""# 도로 노면 점검 보고서 — {detail.get('name', '')} ([{grade_label}])

⚠ **이 보고서는 모의(mock) 데이터 기반입니다.** 실제 AI 노면 손상 탐지 모델은
아직 연동되지 않았습니다(docs/road_surface_management_plan.md 참고). 아래 내용은
화면 구성·기능 검증용이며 실제 도로 상태를 반영하지 않습니다.

| 항목 | 내용 |
|---|---|
| 생성시각 | {now} |
| 위치 | {detail.get('name', '')} |
| 종합 등급 | **{grade_label}** (등급 {grade}/4) |
| 탐지된 손상 | {len(defects)}건 |

## 종합 판단 ({narr_src})
{summary}

## 탐지된 손상 목록
{defects_table}

## 권고 조치
**{recommendation}**

---
*본 보고서는 tot_dashboard 도로 노면 관리 파이프라인이 자동 생성했습니다. 등급·손상
목록·권고는 규칙 기반 **고정 필드**이며, 「종합 판단」 서술만 {narr_src}로 작성됩니다
(서술은 고정 필드를 변경하지 않음).*
"""
