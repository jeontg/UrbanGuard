# -*- coding: utf-8 -*-
"""홈 화면 교통위험 타일(``_traffic_summary``)이 교통위험 미지정 카메라를
지점 수에 세지 않는가 — 2026-08-28 실사용 점검 중 발견.

``_flood_summary()``는 처음부터 ``water_available``로 걸러 "보지 않은
곳을 봤다"고 말하지 않는다. ``_traffic_summary()``에는 그 대칭 게이트가
없어, 침수만 상시로 켠 카메라의 중립 판정(``traffic_enabled=False`` →
``level="관심"``)까지 "이상 없는 지점"으로 세어 지점 수가 부풀려지고
있었다.

⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 교통위험이 별도 프로세스로
옮겨가며 ``_traffic_summary()``가 ``main.store``(제거됨) 대신
``core/live_state.py``를 읽도록 바뀌었다. "미지정 카메라 제외" 게이트도
자리를 옮겼다 — 예전엔 스냅샷의 ``traffic_enabled`` 플래그로 걸렀지만,
이제는 ``BLOCKS``(정적 카메라 목록)에서 ``traffic_enabled`` 인 카메라
id만 먼저 추려 그 id들만 ``live_state``에 묻는다(그래서 미지정
카메라는 애초에 조회 대상에도 안 든다).
"""
from __future__ import annotations

from tot_dashboard.core import live_state
from tot_dashboard.service import main


def _block(block_id: str, *, traffic_enabled: bool) -> dict:
    return {"id": block_id, "traffic_enabled": traffic_enabled}


def _state(*, level: str = "관심", available: bool = True) -> dict:
    return {"level": level, "available": available, "updated_at": None}


def test_교통위험_미지정_카메라는_지점_수에_안_들어간다(monkeypatch):
    monkeypatch.setattr(main, "BLOCKS", [
        _block("A", traffic_enabled=True),
        _block("B", traffic_enabled=False),
    ])
    all_states = {"A": _state(level="주의"), "B": _state(level="관심")}

    # ★ 실제 core/live_state.py::latest_by_camera()는 camera_id IN ids로
    #   걸러 조회한다 — 이 목(mock)도 그 필터링을 그대로 흉내내야 한다.
    #   흉내내지 않으면(ids를 무시하고 전부 돌려주면) "미지정 카메라가
    #   애초에 조회 대상에도 안 든다"는 것 자체를 시험하지 못한다.
    def _fake_latest(ids, domain):
        assert "B" not in ids, "미지정 카메라(traffic_enabled=False)가 조회 대상에 들어갔다"
        return {k: v for k, v in all_states.items() if k in ids}

    monkeypatch.setattr(live_state, "latest_by_camera", _fake_latest)
    r = main._traffic_summary()
    assert r["available"] is True
    assert "지점 2개소" not in r["detail"], (
        "교통위험 미지정 카메라가 지점 수에 섞였다")
    assert "1개소" in r["detail"]


def test_전부_미지정이면_대기중으로_보인다(monkeypatch):
    monkeypatch.setattr(main, "BLOCKS", [_block("A", traffic_enabled=False)])
    called = []
    monkeypatch.setattr(live_state, "latest_by_camera",
                        lambda ids, domain: called.append(ids) or {})
    r = main._traffic_summary()
    assert r["available"] is False
    # ids가 비면 애초에 조회하지 않는다 — 없는 카메라를 물을 이유가 없다.
    assert called == []


def test_DB를_못_읽으면_정상으로_보이지_않는다(monkeypatch):
    """`live_state.latest_by_camera()`가 ``None``(DB 못 읽음)을 돌려주면
    "정상"으로 보이면 안 된다 — 관측이 없는 것과 관측을 못 한 것은
    다르다."""
    monkeypatch.setattr(main, "BLOCKS", [_block("A", traffic_enabled=True)])
    monkeypatch.setattr(live_state, "latest_by_camera", lambda ids, domain: None)
    r = main._traffic_summary()
    assert r["available"] is False


def test_이번_틱에_판정을_못_돈_카메라는_지점_수에_안_들어간다(monkeypatch):
    """``available=False``(재배포 장애 등으로 이번 틱 판정을 안 돌림)인
    카메라는 등급이 있어도 신뢰하면 안 된다."""
    monkeypatch.setattr(main, "BLOCKS", [
        _block("A", traffic_enabled=True),
        _block("B", traffic_enabled=True),
    ])
    monkeypatch.setattr(live_state, "latest_by_camera", lambda ids, domain: {
        "A": _state(level="경계", available=True),
        "B": _state(level="심각", available=False),
    })
    r = main._traffic_summary()
    assert "1개소" in r["detail"]
    assert "「경계」" in r["detail"]
