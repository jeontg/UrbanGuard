"""상·하류 지정과 선행 경고 (core/relations.py).

지켜야 할 것.

* **상·하류는 사람이 넣는다** — 좌표로는 알 수 없다. 그래서 ``auto=False`` 이고
  자동 생성이 건드리지 않는다
* **물이 두 방향으로 흐를 수는 없다** — 반대로 이미 지정돼 있으면 막는다
* **경고 문턱은 이름이 아니라 seq 로 본다** — 기관이 「심각」을 「위험」으로
  바꿔도 문턱이 그대로 맞아야 한다
* ⚠️ **하류에 이미 자기 사건이 있으면 경고하지 않는다** — 아는 일을 다시
  알리면 경보 피로만 쌓인다
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import relations as R
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraLink, Event

PFX = "TEST-FLOW-"


def _purge(db):
    db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
    db.execute(sa_delete(CameraLink).where(
        CameraLink.from_camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
    db.commit()


@pytest.fixture
def db(db_schema):
    s = get_session()
    _purge(s)
    V.seed_builtin(s)
    s.commit()
    yield s
    _purge(s)
    s.close()


def _cams(db):
    """UP(상류) → DOWN(하류). 좌표는 가깝게 둔다."""
    db.add(Camera(id=f"{PFX}UP", name="상류지점", lat=35.1010, lng=129.0300))
    db.add(Camera(id=f"{PFX}DOWN", name="하류지점", lat=35.1000, lng=129.0300))
    db.commit()


def _open_event(db, block_id, level, place=""):
    ev = Event(domain="flood", block_id=block_id, place_name=place or block_id,
               event_type="침수", level=level, status="open")
    db.add(ev)
    db.commit()
    return ev


# --- 지정 -------------------------------------------------------------------


def test_set_flow_creates_both_directions(db):
    """하류를 물어도, 상류를 물어도 답이 나와야 한다."""
    _cams(db)
    assert R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN") == []
    db.commit()

    assert R.downstream_of(db, f"{PFX}UP") == [(f"{PFX}DOWN", 1)]
    up = R.neighbors(db, f"{PFX}DOWN", kinds=("upstream",))
    assert up == [(f"{PFX}UP", 1)]


def test_set_flow_rejects_self(db):
    _cams(db)
    errs = R.set_flow(db, f"{PFX}UP", f"{PFX}UP")
    assert errs and "같은 지점" in errs[0]


def test_set_flow_rejects_unknown_camera(db):
    _cams(db)
    errs = R.set_flow(db, f"{PFX}UP", "NO-SUCH")
    assert errs and "찾을 수 없습니다" in errs[0]


def test_set_flow_rejects_reverse(db):
    """물이 두 방향으로 흐를 수는 없다."""
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()

    errs = R.set_flow(db, f"{PFX}DOWN", f"{PFX}UP")
    assert errs and "반대 방향" in errs[0]


def test_set_flow_is_idempotent(db):
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    n = db.query(CameraLink).filter(
        CameraLink.from_camera_id.like(f"{PFX}%"),
        CameraLink.kind.in_(R.FLOW_KINDS)).count()
    assert n == 2      # downstream 1 + upstream 1


def test_clear_flow_removes_both(db):
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    assert R.clear_flow(db, f"{PFX}UP", f"{PFX}DOWN") == 2
    db.commit()
    assert R.downstream_of(db, f"{PFX}UP") == []


def test_flow_pairs_lists_only_downstream(db):
    """양방향으로 저장하지만 목록에는 한 줄만 나와야 한다."""
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    pairs = [p for p in R.flow_pairs(db) if p["upper_id"].startswith(PFX)]
    assert len(pairs) == 1
    assert pairs[0]["upper_name"] == "상류지점"
    assert pairs[0]["lower_name"] == "하류지점"


def test_auto_rebuild_keeps_flow(db):
    """★ 자동 생성이 사람이 넣은 상·하류를 지우면 안 된다."""
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()

    R.build_adjacency(db, radius_m=500)
    db.commit()
    assert R.downstream_of(db, f"{PFX}UP") == [(f"{PFX}DOWN", 1)]


# --- 선행 경고 --------------------------------------------------------------


def _warn_ids(rows):
    return {r["target_id"] for r in rows if r["target_id"].startswith(PFX)}


def test_warning_when_upstream_is_alert(db):
    """★ 상류에서 「경계」 사건이 열리면 하류를 미리 알린다."""
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    _open_event(db, f"{PFX}UP", "경계", "상류지점")

    got = R.downstream_warnings(db)
    assert _warn_ids(got) == {f"{PFX}DOWN"}
    mine = [r for r in got if r["target_id"] == f"{PFX}DOWN"][0]
    assert mine["source_level"] == "경계"
    assert mine["depth"] == 1


def test_no_warning_below_threshold(db):
    """「주의」는 문턱 아래라 경고하지 않는다."""
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    _open_event(db, f"{PFX}UP", "주의")

    assert _warn_ids(R.downstream_warnings(db)) == set()


def test_threshold_uses_seq_not_name(db):
    """★ 이름이 아니라 seq 로 비교한다.

    기관이 등급 이름을 바꿔도 문턱이 그대로 맞아야 한다.
    """
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    _open_event(db, f"{PFX}UP", "심각")

    # 심각은 seq 4 이므로 문턱 4 에서도 걸린다.
    assert _warn_ids(R.downstream_warnings(db, min_rank=4)) == {f"{PFX}DOWN"}
    # 문턱을 5 로 올리면 아무것도 안 걸린다(5단계 기관을 흉내).
    assert _warn_ids(R.downstream_warnings(db, min_rank=5)) == set()


def test_unknown_level_never_warns(db):
    """어휘에 없는 등급은 rank 0 이라 절대 문턱을 못 넘는다."""
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    _open_event(db, f"{PFX}UP", "미보정")

    assert _warn_ids(R.downstream_warnings(db)) == set()


def test_no_warning_when_downstream_already_busy(db):
    """⚠️ 하류에 이미 자기 사건이 있으면 알리지 않는다 — 경보 피로."""
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    _open_event(db, f"{PFX}UP", "심각")
    _open_event(db, f"{PFX}DOWN", "주의")

    assert _warn_ids(R.downstream_warnings(db)) == set()


def test_closed_event_does_not_warn(db):
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    ev = _open_event(db, f"{PFX}UP", "심각")
    ev.status = "closed"
    db.commit()

    assert _warn_ids(R.downstream_warnings(db)) == set()


def test_adjacent_link_does_not_warn(db):
    """인접일 뿐이면 「다음 차례」가 아니다 — 상·하류만 경고한다."""
    _cams(db)
    db.add(CameraLink(from_camera_id=f"{PFX}UP", to_camera_id=f"{PFX}DOWN",
                      kind="adjacent"))
    db.commit()
    _open_event(db, f"{PFX}UP", "심각")

    assert _warn_ids(R.downstream_warnings(db)) == set()


def test_warning_is_empty_without_events(db):
    _cams(db)
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.commit()
    assert _warn_ids(R.downstream_warnings(db)) == set()


# --- 이벤트 상세용 주변 지점 -------------------------------------------------


def test_nearby_puts_downstream_first(db):
    """하류가 인접보다 앞에 온다 — 「다음에 잠긴다」가 더 중요하다."""
    _cams(db)
    db.add(Camera(id=f"{PFX}SIDE", name="옆지점", lat=35.1010, lng=129.0301))
    db.commit()
    R.set_flow(db, f"{PFX}UP", f"{PFX}DOWN")
    db.add(CameraLink(from_camera_id=f"{PFX}UP", to_camera_id=f"{PFX}SIDE",
                      kind="adjacent"))
    db.commit()

    got = R.nearby_cameras(db, f"{PFX}UP")
    assert [n["id"] for n in got] == [f"{PFX}DOWN", f"{PFX}SIDE"]
    assert got[0]["kind"] == "downstream"
    assert got[0]["name"] == "하류지점"


def test_nearby_respects_limit(db):
    _cams(db)
    for i in range(5):
        cid = f"{PFX}N{i}"
        db.add(Camera(id=cid, name=f"이웃{i}", lat=35.1010, lng=129.0300))
        db.flush()
        db.add(CameraLink(from_camera_id=f"{PFX}UP", to_camera_id=cid,
                          kind="adjacent"))
    db.commit()
    assert len(R.nearby_cameras(db, f"{PFX}UP", limit=3)) == 3


def test_nearby_empty_without_links(db):
    _cams(db)
    assert R.nearby_cameras(db, f"{PFX}UP") == []
