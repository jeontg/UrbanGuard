"""새 이벤트가 **위험유형 코드를 달고 태어나는가**.

## 왜 이 시험이 있나 (2026-08-19 발견)

`events.hazard_type_code` 를 **채우는 곳이 없었다.** 유형 어휘(`hazard_types`)
11행을 만들어 두고, 정작 이벤트가 그것을 가리키지 않았다. 확인해 보니 31건
**전부** 비어 있었다.

⚠️ 비어 있어도 **아무 일도 안 일어난다.** 화면은 도메인으로 잘 돌아간다.
그래서 유형 기준으로 묶으려 할 때가 되어서야 「지난 이벤트가 통째로 빠진다」는
것이 드러난다. **그때는 이미 늦다.**

★ 그리고 채울 수 있는 것은 **대분류까지**다. 「지하차도 침수」인지 「배수로
침수」인지는 지나간 이벤트로는 알 수 없다. 그 한계를 시험으로도 못박는다 —
나중에 누군가 「세분류도 도메인에서 채우자」고 하면 안 되기 때문이다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import events as EV
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Event

PFX = "TEST-HZ-"


@pytest.fixture
def db(db_schema):
    s = get_session()
    s.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
    s.commit()
    V.seed_builtin(s)
    s.commit()
    yield s
    s.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
    s.commit()
    s.close()


def _make(db, domain: str, level: str = "심각", **kw):
    ev = EV.record_detection(db, domain=domain, block_id=f"{PFX}{domain}",
                             place_name="시험지점", level=level, **kw)
    db.commit()
    return ev


# --- 도출 규칙 (DB 없이) ----------------------------------------------------


def test_도메인에서_대분류를_도출한다():
    assert V.base_hazard_for("flood") == "flood"
    assert V.base_hazard_for("crowd") == "crowd"
    assert V.base_hazard_for("road") == "road"


def test_모르는_도메인은_빈_값이다():
    """⚠️ 아무 코드나 붙이면 안 된다 — 없는 것이 틀린 것보다 낫다."""
    assert V.base_hazard_for("wildfire") == ""
    assert V.base_hazard_for("") == ""


def test_어휘에_없는_코드는_버린다():
    """⚠️ 모르는 코드를 저장하면 **어디에도 안 속하는 이벤트**가 된다.

    유형별로 묶을 때 조용히 통계에서 빠진다.
    """
    assert V.normalize_hazard_code("flood_underpass", "flood") == "flood_underpass"
    assert V.normalize_hazard_code("아무거나", "flood") == "flood"
    assert V.normalize_hazard_code("", "crowd") == "crowd"
    assert V.normalize_hazard_code("아무거나", "") == ""


# --- 실제 이벤트 ------------------------------------------------------------


@pytest.mark.parametrize("domain,expect",
                         [("flood", "flood"), ("crowd", "crowd"),
                          ("road", "road")])
def test_새_이벤트에_유형_코드가_붙는다(db, domain, expect):
    ev = _make(db, domain)
    assert ev is not None
    assert ev.hazard_type_code == expect


def test_세분류를_넘기면_그대로_쓴다(db):
    """★ 탐지기가 더 아는 경우에는 대분류로 뭉개지 않는다."""
    ev = _make(db, "flood", hazard_type_code="flood_underpass")
    assert ev.hazard_type_code == "flood_underpass"


def test_세분류를_도메인에서_지어내지_않는다(db):
    """⚠️ 여기가 이 시험 묶음의 요점이다.

    도메인만 알 때 「지하차도 침수」로 찍으면, 그 값으로 갈리는 SOP 가
    **틀린 절차를 안내한다.** 절차가 없는 것보다 나쁘다.
    """
    ev = _make(db, "flood")
    assert ev.hazard_type_code == "flood"
    assert ev.hazard_type_code != "flood_underpass"


def test_기존_이벤트를_갱신할_때는_건드리지_않는다(db):
    """이미 붙은 세분류를 다음 탐지가 대분류로 덮으면 정보가 줄어든다."""
    first = _make(db, "flood", hazard_type_code="flood_drainage")
    assert first.hazard_type_code == "flood_drainage"
    again = _make(db, "flood")          # 같은 지점 → 갱신 경로
    assert again.id == first.id
    assert again.hazard_type_code == "flood_drainage"


def test_도메인과_유형이_어긋나지_않는다(db):
    """유형의 `domain` 이 이벤트의 `domain` 과 같아야 한다."""
    by_code = {c: d for c, d, *_ in V.DEFAULT_HAZARD_TYPES}
    for domain in ("flood", "crowd", "road"):
        ev = _make(db, domain)
        assert by_code[ev.hazard_type_code] == domain
