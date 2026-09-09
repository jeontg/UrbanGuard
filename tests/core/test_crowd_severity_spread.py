"""인파 등급이 실제로 갈리는가 (2026-08-19 전수조사).

## 왜 이 시험이 있나

실측 결과 인파 관측 **670회 중 627회(93.6%)가 같은 등급**이었다.

원인을 찾아보니 판정 문턱 ``disp_hi=0.6`` 이 방아쇠인데, 실측 ``dispersion``
평균이 **0.888**(94%가 문턱 초과)이었다. 다른 지표는 문턱 근처도 가지 않았다
(surge 1.026 / 문턱 1.6, divergence 0.042 / 문턱 6.0).

⚠️ **계산이 틀린 것이 아니다.** ``dispersion = 1 - ‖평균 단위벡터‖`` 라
사람들이 여러 방향으로 가면 1 에 가까워진다. **교차로는 본래 흐름이 갈린다.**

★ **등급이 늘 같으면 그 등급은 정보가 아니다.** 관제요원이 「지금 평소와
다른가」에 답할 수 없다. 이 시험들은 **쏠림이 화면까지 드러나는지**를 지킨다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import crowd_history as CH
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import CrowdObservation

PFX = "TEST-CSS-"


def _purge(db):
    db.execute(sa_delete(CrowdObservation).where(
        CrowdObservation.camera_id.like(f"{PFX}%")))
    db.commit()


@pytest.fixture
def db(db_schema):
    s = get_session()
    _purge(s)
    yield s
    _purge(s)
    s.close()


def _obs(db, *, severity: int, failed: bool = False):
    CH.record(db, camera_id=f"{PFX}A", camera_name="시험지점",
              snapshot={"person_count": 10, "density_index": 0.1,
                        "mean_speed": 1.0, "surge": 1.0, "dispersion": 0.9,
                        "divergence": 0.0, "risk_code": "FLOW_CHAOS",
                        "risk_score": 0.2, "severity": severity,
                        "drivers": []},
              source="test", failed=failed)


def test_쏠림을_센다(db):
    for _ in range(9):
        _obs(db, severity=2)
    _obs(db, severity=0)
    db.commit()
    s = CH.severity_spread(db, f"{PFX}A")
    assert s["total"] == 10
    assert s["top_severity"] == 2
    assert s["top_ratio"] == pytest.approx(0.9)


def test_실패한_관측은_빼고_센다(db):
    """못 본 것을 등급으로 세면 쏠림이 실제와 달라진다."""
    _obs(db, severity=2)
    _obs(db, severity=0, failed=True)
    db.commit()
    s = CH.severity_spread(db, f"{PFX}A")
    assert s["total"] == 1


def test_관측이_없으면_쏠림을_말하지_않는다(db):
    """★ 0/0 을 「고르다」로 답하면 **안 봤는데 정상인 것처럼** 보인다."""
    s = CH.severity_spread(db, f"{PFX}없는지점")
    assert s["total"] == 0
    assert s["top_ratio"] is None


def test_화면이_쏠림을_말한다():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
          / "service" / "static" / "app.js").read_text(encoding="utf-8")
    assert "_crowdSpreadHtml" in js
    assert "같은 등급" in js
    # ★ 「그래서 무엇을 해야 하나」까지 말해야 한다.
    assert "문턱 조정" in js
