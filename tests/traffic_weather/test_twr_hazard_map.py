"""TWR_* 판정코드 → traffic_* 위험유형 매핑 (2026-08-26 신설).

## 왜 이 시험이 있나

`ontology.TWR_TO_HAZARD_TYPE`이 판정축(TWR_*)과 유형축(traffic_*) 사이의
단방향 번역표다. 이게 없으면 `event_sync._sync_traffic()`이 만드는 이벤트가
전부 대분류 "traffic"으로 뭉개져, 위험유형 어휘에 이미 있던 4종이 한 번도
채워지지 않는다(`docs/202608260842/` 에서 확인한 실제 결함).

매핑이 누락되면 조용히 대분류로 떨어질 뿐 예외가 나지 않는다 — 그래서
"모든 TWR 코드가 매핑표에 있다"를 직접 시험으로 못박는다.
"""
from __future__ import annotations

from tot_dashboard.core import vocabulary as V
from tot_dashboard.traffic_weather.knowledge.ontology import (
    TRAFFIC_RISK_CATALOG, TWR_TO_HAZARD_TYPE)


def test_모든_TWR코드가_매핑표에_있다():
    """카탈로그에 새 TWR_* 코드를 추가하고 매핑을 깜빡하면 여기서 바로
    걸린다 — 조용히 대분류로 떨어지는 대신."""
    assert set(TRAFFIC_RISK_CATALOG) == set(TWR_TO_HAZARD_TYPE)


def test_매핑된_유형코드는_전부_공용어휘에_있다():
    """어휘에 없는 코드를 매핑하면 normalize_hazard_code() 가 조용히
    버리고 대분류로 떨어뜨린다 — 매핑한 의미가 없어진다."""
    for hazard_code in TWR_TO_HAZARD_TYPE.values():
        if not hazard_code:  # TWR_NORMAL은 매핑하지 않는다(이벤트가 안 됨)
            continue
        assert hazard_code in V.KNOWN_HAZARD_CODES, (
            f"{hazard_code} 가 어휘(DEFAULT_HAZARD_TYPES)에 없습니다")


def test_TWR_NORMAL은_매핑하지_않는다():
    """관심 등급이라 애초에 이벤트가 되지 않는다 — 매핑이 있어도 안 있어도
    관측되지 않는 값이지만, 실수로 유형을 붙이면 오해를 낳는다."""
    assert TWR_TO_HAZARD_TYPE["TWR_NORMAL"] == ""


def test_대기열_지연은_TWR코드와_대응하지_않는다():
    """`traffic_queue_delay`는 의도적으로 매핑하지 않은 상태로 남는다 —
    없는 대응을 억지로 만들면 통계가 섞인다(ontology.py 주석 참고)."""
    assert "traffic_queue_delay" not in TWR_TO_HAZARD_TYPE.values()
