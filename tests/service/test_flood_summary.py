# -*- coding: utf-8 -*-
"""홈 화면 침수 타일(``_flood_summary``) — API 게이트웨이 Phase 4 이후.

⚠️ 2026-08-31 신설 — 침수가 별도 프로세스(flood-service)로 옮겨가며
``_flood_summary()``가 ``main.store``(제거됨) 대신 ``core/live_state.py``
를 읽도록 바뀌었다. ``test_traffic_summary_excludes_disabled.py``와
같은 패턴 — flood-service가 이미 행안부 4단계로 매핑해 저장해 두므로
(``_FLOOD_GRADE_LEVEL``, ``event_sync.py``와 같은 매핑) 여기서 다시
등급을 변환하지 않는다는 것도 함께 확인한다.
"""
from __future__ import annotations

from tot_dashboard.core import live_state
from tot_dashboard.service import main


def _block(block_id: str, *, flood_enabled: bool) -> dict:
    return {"id": block_id, "flood_enabled": flood_enabled}


def _state(*, level: str = "관심", available: bool = True) -> dict:
    return {"level": level, "available": available, "updated_at": None}


def test_침수_미지정_카메라는_지점_수에_안_들어간다(monkeypatch):
    monkeypatch.setattr(main, "BLOCKS", [
        _block("A", flood_enabled=True),
        _block("B", flood_enabled=False),
    ])
    monkeypatch.setattr(live_state, "latest_by_camera", lambda ids, domain: {
        "A": _state(level="경계"),
    })
    r = main._flood_summary()
    assert r["available"] is True
    assert "관측 1개소" in r["detail"]


def test_전부_미지정이면_대기중으로_보인다(monkeypatch):
    monkeypatch.setattr(main, "BLOCKS", [_block("A", flood_enabled=False)])
    monkeypatch.setattr(live_state, "latest_by_camera", lambda ids, domain: {})
    r = main._flood_summary()
    assert r["available"] is False


def test_DB를_못_읽으면_정상으로_보이지_않는다(monkeypatch):
    monkeypatch.setattr(main, "BLOCKS", [_block("A", flood_enabled=True)])
    monkeypatch.setattr(live_state, "latest_by_camera", lambda ids, domain: None)
    r = main._flood_summary()
    assert r["available"] is False


def test_이번_틱에_판정을_못_돈_카메라는_지점_수에_안_들어간다(monkeypatch):
    monkeypatch.setattr(main, "BLOCKS", [
        _block("A", flood_enabled=True),
        _block("B", flood_enabled=True),
    ])
    monkeypatch.setattr(live_state, "latest_by_camera", lambda ids, domain: {
        "A": _state(level="심각", available=True),
        "B": _state(level="심각", available=False),
    })
    r = main._flood_summary()
    assert "관측 1개소" in r["detail"]
    assert "「심각」" in r["detail"]


def test_alert_레벨이면_headline에_건수가_찍힌다(monkeypatch):
    monkeypatch.setattr(main, "BLOCKS", [_block("A", flood_enabled=True)])
    monkeypatch.setattr(live_state, "latest_by_camera", lambda ids, domain: {
        "A": _state(level="심각"),
    })
    r = main._flood_summary()
    assert r["headline"] == "1건"
