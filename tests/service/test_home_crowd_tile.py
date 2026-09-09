"""홈 화면 인파관리 타일이 실제 위험도를 읽는가 (2026-08-22 전수점검).

**신고받은 문제.** ``_crowd_summary()``(예전 이름 없이 ``home()`` 안에
인라인으로 있었다)가 ``_crowd_analyzer is not None`` 여부만 보고 항상
"동작 중"(초록)을 표시했다 — 「패닉분산」(최고 심각도)이 진행 중이어도
홈 타일은 계속 초록이었다. 다른 3개 도메인 타일(``_traffic_summary``·
``_flood_summary``·``_road_summary``)은 실제 관측에서 등급을 계산하는데,
인파만 거짓을 말하고 있었다.

⚠️ 2026-08-31 — API 게이트웨이 Phase 1로 ``_crowd_analyzer``가 이
프로세스(main.py)에서 아예 사라졌다(별도 서비스 crowd_service.py로
이관). ``_crowd_summary()``는 애초에 그 분석기를 다시 부르지 않고
DB(``CrowdObservation``)만 읽고 있었으므로, "분석기가 켜져 있는가"를
더는 확인하지 않고 곧바로 DB를 조회하도록 바뀌었다 — 계획서가 의도한
대로("홈 화면은 도메인 서비스 프로세스 상태에 안 걸리게") 오히려 더
견고해졌다. 그래서 "분석기 꺼져있으면 비활성" 시험은 **더 이상 존재할
수 없는 상태를 테스트하던 것**이라 지웠다 — 이제 인파관리 서비스가
죽어 있어도 이 타일은 DB에 남은 마지막 관측을 그대로 보여준다(실기로
확인함, 이 회차).
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import CrowdObservation, Event, EventAction
from tot_dashboard.service import event_sync, main

CAM = "TEST-HCT-A"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(EventAction).where(
            EventAction.event_id.in_(
                select(Event.id).where(Event.block_id == CAM))))
        db.execute(sa_delete(Event).where(Event.block_id == CAM))
        # ⚠️ 이 카메라 것만 지우면 안 된다 — `_crowd_summary()` 는 **전체
        # 카메라를 집계**하는 함수라, 다른 시험 파일(예: test_crowd_
        # density_event.py 의 CAM="TEST-CDE-A")이 남긴 관측이 섞이면 결과
        # 건수가 달라진다(전체 시험 스위트에서만 재현되던 문제, 2026-08-22
        # 발견). 이 테이블 전체를 비우는 것이 맞다 — test_roi_persistence.py
        # 가 Camera 관련 표를 통째로 비우는 것과 같은 이유다.
        db.execute(sa_delete(CrowdObservation))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


def _snap(severity: int) -> dict:
    return {"severity": severity, "person_count": 20, "density_index": 0.8,
            "risk_name": "패닉분산", "risk_code": "CROWD_PANIC",
            "risk_score": 0.9, "drivers": ["급증"]}


def test_최근_관측이_없으면_대기중(monkeypatch):
    r = main._crowd_summary()
    assert r["headline"] == "관측 대기"


def test_평온한_관측은_초록(monkeypatch):
    event_sync.record_crowd_observation(CAM, "시험지점", _snap(0))
    r = main._crowd_summary()
    assert r["headline"] == "정상"
    assert r["color"] == "#3fb950"


def test_패닉분산이면_더이상_초록이_아니다(monkeypatch):
    """★ 핵심 회귀 시험 — 예전에는 이 경우에도 계속 초록·"동작 중"이었다."""
    event_sync.record_crowd_observation(CAM, "시험지점", _snap(4))
    r = main._crowd_summary()
    assert r["color"] != "#3fb950"
    assert r["headline"] != "동작 중"
    assert "심각" in r["detail"]
