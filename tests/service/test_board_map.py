"""상황판 지도 좌표 정규화 (S-01 ②영역).

외부 지도 서비스를 쓰지 않으므로 좌표 → 화면 위치 변환이 우리 책임이다.
어긋나면 관제요원이 엉뚱한 지점을 보게 된다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.service import board_map as M

BUSAN = [
    {"id": "A", "name": "부산역", "coordinates": {"lat": 35.1150, "lng": 129.0394}},
    {"id": "B", "name": "재송", "coordinates": {"lat": 35.1915, "lng": 129.1200}},
    {"id": "C", "name": "범내골", "coordinates": {"lat": 35.1473, "lng": 129.0591}},
]


def test_all_points_land_inside_the_viewport():
    for p in M.build_points(BUSAN):
        assert M.PAD <= p["x"] <= 100 - M.PAD
        assert M.PAD <= p["y"] <= 100 - M.PAD


def test_north_is_up():
    """위도가 큰 지점이 화면 위쪽(y가 작음)에 와야 한다."""
    pts = {p["block_id"]: p for p in M.build_points(BUSAN)}
    assert pts["B"]["y"] < pts["C"]["y"] < pts["A"]["y"]


def test_east_is_right():
    pts = {p["block_id"]: p for p in M.build_points(BUSAN)}
    assert pts["A"]["x"] < pts["C"]["x"] < pts["B"]["x"]


def test_single_block_is_centered_not_divided_by_zero():
    """지점이 하나면 경계 상자 폭이 0이 되어 0으로 나누기가 난다."""
    pts = M.build_points([{"id": "S", "name": "단일",
                           "coordinates": {"lat": 35.1, "lng": 129.0}}])
    assert len(pts) == 1
    assert pts[0]["x"] == pytest.approx(50.0, abs=0.1)
    assert pts[0]["y"] == pytest.approx(50.0, abs=0.1)


def test_blocks_without_coordinates_are_skipped():
    pts = M.build_points(BUSAN + [{"id": "X", "name": "좌표없음"}])
    assert {p["block_id"] for p in pts} == {"A", "B", "C"}


def test_no_coordinates_at_all_returns_empty():
    assert M.build_points([{"id": "X", "name": "좌표없음"}]) == []


def test_points_without_events_show_as_normal():
    """이벤트가 없는 지점도 지도에 남아야 한다.

    지점이 사라지면 관제요원은 정상인지 고장인지 구분할 수 없다.
    """
    for p in M.build_points(BUSAN, db=None):
        assert p["alert"] is False
        assert p["status"] == "정상"
        assert p["color"] == M.NORMAL_COLOR


def test_event_level_colors_the_point(db_schema):
    from tot_dashboard.core import events as E
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import Event, EventAction
    db = get_session()
    try:
        db.query(EventAction).delete()
        db.query(Event).delete()
        E.record_detection(db, domain="flood", block_id="A",
                           place_name="부산역", level="심각")
        db.commit()
        pts = {p["block_id"]: p for p in M.build_points(BUSAN, db)}
        assert pts["A"]["alert"] is True
        assert pts["A"]["color"] == M.LEVEL_COLOR["심각"]
        assert pts["A"]["event_id"] is not None
        assert pts["B"]["alert"] is False
    finally:
        db.query(EventAction).delete()
        db.query(Event).delete()
        db.commit()
        db.close()


def test_highest_level_wins_when_a_block_has_several_events(db_schema):
    """한 지점에 여러 도메인 이벤트가 있으면 가장 높은 등급을 보여 준다."""
    from tot_dashboard.core import events as E
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import Event, EventAction
    db = get_session()
    try:
        db.query(EventAction).delete()
        db.query(Event).delete()
        E.record_detection(db, domain="flood", block_id="A",
                           place_name="부산역", level="주의")
        E.record_detection(db, domain="crowd", block_id="A",
                           place_name="부산역", level="경계")
        db.commit()
        pts = {p["block_id"]: p for p in M.build_points(BUSAN, db)}
        assert pts["A"]["level"] == "경계"
    finally:
        db.query(EventAction).delete()
        db.query(Event).delete()
        db.commit()
        db.close()
