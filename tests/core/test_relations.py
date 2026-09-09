"""관계 모델 (core/relations.py) — Urban Ontology 1단계.

지켜야 할 것.

* **비어 있어도 기존 동작이 그대로다** — 관계 표는 전부 덧붙인 것이라,
  행이 하나도 없을 때 조회가 예외 없이 빈 목록을 줘야 한다
* **인접은 양방향, 상·하류는 단방향** — A 의 하류가 B 라고 B 의 하류가 A 는
  아니다. 이걸 섞으면 「상류가 차오르면 하류를 미리 경고」가 거꾸로 돈다
* **자동 생성은 ``auto=True`` 만 지운다** — 사람이 넣은 상·하류까지 지우면
  복구할 방법이 없다(좌표만으로는 알 수 없는 관계다)
* **홉 수 제한이 반드시 걸린다** — 링크가 순환하면 재귀가 끝나지 않는다
* **어휘 초기값이 현재 코드와 일치한다** — ``calibration.py`` 가 반환하는
  「관심·주의·경계·심각」과 label 이 어긋나면 화면과 표가 따로 논다
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import relations as R
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (Camera, CameraLink, CameraSensor,
                                       CameraZone, HazardType, RiskLevel,
                                       Sensor, Zone)

PFX = "TEST-REL-"


def _purge(db):
    db.execute(sa_delete(CameraLink).where(
        CameraLink.from_camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(CameraSensor).where(
        CameraSensor.camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(CameraZone).where(
        CameraZone.camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
    db.execute(sa_delete(Sensor).where(Sensor.id.like(f"{PFX}%")))
    db.execute(sa_delete(Zone).where(Zone.id.like(f"{PFX}%")))
    db.commit()


@pytest.fixture
def db(db_schema):
    s = get_session()
    _purge(s)
    # 시험 DB 는 속도 때문에 create_all 을 써서 마이그레이션을 타지 않는다.
    # 어휘는 같은 목록(core/vocabulary.py)에서 심는다.
    V.seed_builtin(s)
    s.commit()
    yield s
    _purge(s)
    s.close()


def _cam(db, suffix, lat, lng):
    c = Camera(id=f"{PFX}{suffix}", name=f"시험지점 {suffix}", lat=lat, lng=lng)
    db.add(c)
    return c


# --- 어휘 -------------------------------------------------------------------


def test_risk_levels_seeded(db):
    """위험등급 4행이 심어져 있고 label 이 현재 코드값과 같다.

    ⚠️ **성격이 `risk` 인 것만 본다.** 2026-08-19 부터 같은 표에 노면
    **정비 등급**(`maintenance`)도 들어 있다 — 그쪽은 위험등급이 아니라
    이벤트 비교·경보에 끼면 안 된다.
    """
    rows = (db.query(RiskLevel).filter(RiskLevel.kind == "risk")
            .order_by(RiskLevel.seq).all())
    assert [r.code for r in rows] == ["interest", "caution", "alert", "severe"]
    # calibration.py 가 반환하는 문자열과 정확히 같아야 한다.
    assert [r.label for r in rows] == ["관심", "주의", "경계", "심각"]
    # 단독 발송 허용은 「심각」 하나뿐이다(roles.CRITICAL_LEVELS 와 일치).
    assert [r.code for r in rows if r.is_critical] == ["severe"]


def test_정비등급은_위험등급과_섞이지_않는다(db):
    """★ 노면 「긴급」이 위험등급으로 새면 정비 대상이 즉시 경보된다."""
    maint = db.query(RiskLevel).filter(RiskLevel.kind == "maintenance").all()
    assert {r.code for r in maint} == {"road_good", "road_watch",
                                       "road_repair", "road_urgent"}
    # 정비 등급에는 단독 발송이라는 개념 자체가 없다.
    assert not any(r.is_critical for r in maint)


def test_hazard_types_seeded_and_hierarchical(db):
    """위험유형이 계층으로 심어져 있다."""
    types = {h.code: h for h in db.query(HazardType).all()}
    assert types["flood_underpass"].parent_code == "flood"
    assert types["flood_underpass"].source == "rfp"   # 경남 SFR-008 근거
    assert types["flood"].parent_code is None


def test_undetectable_types_are_marked(db):
    """⚠️ 어휘 등록과 탐지 가능은 다르다.

    산불·태풍은 발주요구라 어휘로 넣되 모델이 없다. 이 구분이 없으면 화면이
    「우리는 산불도 탐지한다」는 거짓말을 만든다.
    """
    types = {h.code: h for h in db.query(HazardType).all()}
    assert types["wildfire"].detectable is False
    assert types["typhoon_damage"].detectable is False
    assert types["flood_underpass"].detectable is True


# --- 비어 있을 때 -----------------------------------------------------------


def test_queries_are_safe_when_empty(db):
    """관계가 하나도 없어도 예외 없이 빈 목록을 준다."""
    _cam(db, "A", 35.10, 129.03)
    db.commit()
    assert R.neighbors(db, f"{PFX}A") == []
    assert R.downstream_of(db, f"{PFX}A") == []
    assert R.sensors_for(db, f"{PFX}A") == []
    assert R.zones_under(db, "") == []


def test_neighbors_of_unknown_camera(db):
    """없는 카메라를 물어도 빈 목록이다 — 예외로 화면이 깨지면 안 된다."""
    assert R.neighbors(db, "NO-SUCH-CAMERA") == []


# --- 인접 탐색 --------------------------------------------------------------


def _chain(db):
    """A ─ B ─ C 사슬. A와 C 는 직접 이어져 있지 않다."""
    _cam(db, "A", 35.100, 129.030)
    _cam(db, "B", 35.101, 129.031)
    _cam(db, "C", 35.102, 129.032)
    for x, y in (("A", "B"), ("B", "A"), ("B", "C"), ("C", "B")):
        db.add(CameraLink(from_camera_id=f"{PFX}{x}", to_camera_id=f"{PFX}{y}",
                          kind="adjacent"))
    db.commit()


def test_neighbors_one_hop(db):
    _chain(db)
    got = R.neighbors(db, f"{PFX}A", max_hop=1)
    assert got == [(f"{PFX}B", 1)]


def test_neighbors_two_hop_reaches_c(db):
    _chain(db)
    got = dict(R.neighbors(db, f"{PFX}A", max_hop=2))
    assert got == {f"{PFX}B": 1, f"{PFX}C": 2}


def test_hop_limit_is_enforced(db):
    """⚠️ 링크가 순환해도 끝난다. 깊이 제한이 유일한 안전장치다."""
    _chain(db)
    # A ↔ B ↔ C 는 이미 순환이다(양방향이므로).
    got = R.neighbors(db, f"{PFX}A", max_hop=1)
    assert len(got) == 1
    assert R.neighbors(db, f"{PFX}A", max_hop=0) == []


def test_shortest_depth_wins(db):
    """여러 경로로 닿으면 가장 짧은 깊이를 준다."""
    _chain(db)
    db.add(CameraLink(from_camera_id=f"{PFX}A", to_camera_id=f"{PFX}C",
                      kind="adjacent"))
    db.commit()
    assert dict(R.neighbors(db, f"{PFX}A", max_hop=2))[f"{PFX}C"] == 1


# --- 상·하류 ---------------------------------------------------------------


def test_downstream_is_directional(db):
    """★ 상·하류는 단방향이다.

    A 의 하류가 B 라고 해서 B 의 하류가 A 는 아니다. 이게 뒤집히면
    「상류가 차오르면 하류를 미리 경고」가 거꾸로 돈다.
    """
    _cam(db, "UP", 35.100, 129.030)
    _cam(db, "DOWN", 35.090, 129.030)
    db.add(CameraLink(from_camera_id=f"{PFX}UP", to_camera_id=f"{PFX}DOWN",
                      kind="downstream"))
    db.commit()

    assert R.downstream_of(db, f"{PFX}UP") == [(f"{PFX}DOWN", 1)]
    assert R.downstream_of(db, f"{PFX}DOWN") == []


def test_adjacent_query_ignores_flow_links(db):
    """인접을 물었는데 상·하류가 섞여 나오면 안 된다."""
    _cam(db, "UP", 35.100, 129.030)
    _cam(db, "DOWN", 35.090, 129.030)
    db.add(CameraLink(from_camera_id=f"{PFX}UP", to_camera_id=f"{PFX}DOWN",
                      kind="downstream"))
    db.commit()
    assert R.neighbors(db, f"{PFX}UP") == []


# --- 자동 생성 --------------------------------------------------------------


def test_build_adjacency_creates_both_directions(db):
    """A 가 B 의 이웃이면 B 도 A 의 이웃이다."""
    _cam(db, "A", 35.1000, 129.0300)
    _cam(db, "B", 35.1010, 129.0300)      # 약 111m
    db.commit()

    R.build_adjacency(db, radius_m=300)
    db.commit()
    assert R.neighbors(db, f"{PFX}A") == [(f"{PFX}B", 1)]
    assert R.neighbors(db, f"{PFX}B") == [(f"{PFX}A", 1)]


def test_build_adjacency_respects_radius(db):
    _cam(db, "A", 35.1000, 129.0300)
    _cam(db, "B", 35.1100, 129.0300)      # 약 1.1km
    db.commit()

    R.build_adjacency(db, radius_m=300)
    db.commit()
    assert R.neighbors(db, f"{PFX}A") == []


def test_build_adjacency_keeps_manual_links(db):
    """★ 자동 생성은 ``auto=True`` 만 지운다.

    사람이 넣은 상·하류는 좌표만으로 복원할 수 없다. 자동 생성을 다시 돌릴
    때마다 사라지면 아무도 쓰지 않는다.
    """
    _cam(db, "A", 35.1000, 129.0300)
    _cam(db, "B", 35.1010, 129.0300)
    db.add(CameraLink(from_camera_id=f"{PFX}A", to_camera_id=f"{PFX}B",
                      kind="downstream", auto=False, note="사람이 지정"))
    db.commit()

    R.build_adjacency(db, radius_m=300)
    db.commit()
    R.build_adjacency(db, radius_m=300)   # 두 번 돌려도
    db.commit()

    assert R.downstream_of(db, f"{PFX}A") == [(f"{PFX}B", 1)]
    manual = db.query(CameraLink).filter_by(
        from_camera_id=f"{PFX}A", kind="downstream").one()
    assert manual.note == "사람이 지정"


def test_build_adjacency_is_idempotent(db):
    """두 번 돌려도 링크가 두 배가 되지 않는다."""
    _cam(db, "A", 35.1000, 129.0300)
    _cam(db, "B", 35.1010, 129.0300)
    db.commit()

    R.build_adjacency(db, radius_m=300)
    db.commit()
    first = db.query(CameraLink).filter(
        CameraLink.from_camera_id.like(f"{PFX}%")).count()
    R.build_adjacency(db, radius_m=300)
    db.commit()
    second = db.query(CameraLink).filter(
        CameraLink.from_camera_id.like(f"{PFX}%")).count()
    assert first == second == 2


def test_dry_run_writes_nothing(db):
    _cam(db, "A", 35.1000, 129.0300)
    _cam(db, "B", 35.1010, 129.0300)
    db.commit()

    got = R.build_adjacency(db, radius_m=300, dry_run=True)
    db.commit()
    assert got["links"] == 2 and got["dry_run"] is True
    assert R.neighbors(db, f"{PFX}A") == []


def test_zero_distance_pairs_are_reported(db):
    """⚠️ 좌표가 같은 지점이 실제로 있다(광복로/광복로1 데이터 결함).

    자동 생성이 그걸 조용히 이웃으로 묶으면 안 되고, **세어서 알려야** 한다.
    좌표를 지어내 고치지는 않는다.
    """
    _cam(db, "A", 35.1000, 129.0300)
    _cam(db, "B", 35.1000, 129.0300)      # 같은 좌표
    db.commit()

    got = R.build_adjacency(db, radius_m=300, dry_run=True)
    assert got["zero_distance_pairs"] == 1


def test_cameras_without_coords_are_skipped(db):
    """좌표가 없으면 이웃을 만들 수 없다 — 예외가 아니라 건너뛴다."""
    _cam(db, "A", 35.1000, 129.0300)
    db.add(Camera(id=f"{PFX}NOCOORD", name="좌표 없음"))
    db.commit()

    got = R.build_adjacency(db, radius_m=5000, dry_run=True)
    assert got["cameras"] == 1
    assert got["links"] == 0


# --- 거리·방위 --------------------------------------------------------------


def test_haversine_matches_known_distance(db=None):
    """위도 0.01도 ≈ 1.11km."""
    d = R.haversine_m(35.10, 129.03, 35.11, 129.03)
    assert 1100 < d < 1120


def test_bearing_cardinal_directions(db=None):
    assert R.bearing_deg(35.10, 129.03, 35.11, 129.03) == 0      # 북
    assert R.bearing_deg(35.10, 129.03, 35.09, 129.03) == 180    # 남
    assert 89 <= R.bearing_deg(35.10, 129.03, 35.10, 129.04) <= 91   # 동


# --- 센서 -------------------------------------------------------------------


def test_sensor_can_serve_many_cameras(db):
    """★ 경남 SFR-005 「1:1 또는 N:1 로 매핑」 — 센서 하나에 카메라 여럿."""
    _cam(db, "A", 35.100, 129.030)
    _cam(db, "B", 35.101, 129.031)
    db.add(Sensor(id=f"{PFX}S1", kind="water_level", name="시험 수위계"))
    db.flush()
    db.add(CameraSensor(camera_id=f"{PFX}A", sensor_id=f"{PFX}S1",
                        role="primary"))
    db.add(CameraSensor(camera_id=f"{PFX}B", sensor_id=f"{PFX}S1",
                        role="reference"))
    db.commit()

    cams = R.cameras_for_sensor(db, f"{PFX}S1")
    assert [c.id for c in cams] == [f"{PFX}A", f"{PFX}B"]


def test_primary_sensor_comes_first(db):
    """참고 센서까지 뒤섞여 나오면 관제요원이 무엇을 볼지 알 수 없다."""
    _cam(db, "A", 35.100, 129.030)
    db.add(Sensor(id=f"{PFX}S1", kind="rain_gauge", name="참고 우량계"))
    db.add(Sensor(id=f"{PFX}S2", kind="water_level", name="대표 수위계"))
    db.flush()
    db.add(CameraSensor(camera_id=f"{PFX}A", sensor_id=f"{PFX}S1",
                        role="reference"))
    db.add(CameraSensor(camera_id=f"{PFX}A", sensor_id=f"{PFX}S2",
                        role="primary"))
    db.commit()

    got = R.sensors_for(db, f"{PFX}A")
    assert [s.id for s, _ in got] == [f"{PFX}S2", f"{PFX}S1"]
    assert R.sensors_for(db, f"{PFX}A", primary_only=True)[0][0].id == f"{PFX}S2"


# --- 구역 -------------------------------------------------------------------


def test_zones_under_prefix(db):
    db.add(Zone(id=f"{PFX}Z1", kind="admin", path="kr.gyeongnam",
                name="경상남도"))
    db.add(Zone(id=f"{PFX}Z2", kind="admin", path="kr.gyeongnam.changwon",
                name="창원시"))
    db.add(Zone(id=f"{PFX}Z3", kind="admin", path="kr.busan", name="부산광역시"))
    db.commit()

    got = {z.id for z in R.zones_under(db, "kr.gyeongnam")}
    assert got == {f"{PFX}Z1", f"{PFX}Z2"}      # 자기 자신 포함, 부산 제외


def test_zones_under_does_not_match_sibling_prefix(db):
    """``kr.busan`` 이 ``kr.busanjin`` 을 끌어오면 안 된다."""
    db.add(Zone(id=f"{PFX}Z1", kind="admin", path="kr.busan", name="부산"))
    db.add(Zone(id=f"{PFX}Z2", kind="admin", path="kr.busanjin", name="딴 곳"))
    db.commit()

    got = {z.id for z in R.zones_under(db, "kr.busan")}
    assert got == {f"{PFX}Z1"}


def test_cameras_in_zone_reports_coverage(db):
    """가장자리만 걸친 카메라를 전체로 세면 커버리지가 부풀려진다."""
    _cam(db, "A", 35.100, 129.030)
    db.add(Zone(id=f"{PFX}Z1", kind="hazard", path="hz.flood.test",
                name="시험 침수구역", source="침수흔적도"))
    db.flush()
    db.add(CameraZone(camera_id=f"{PFX}A", zone_id=f"{PFX}Z1", coverage="edge"))
    db.commit()

    got = R.cameras_in_zone(db, f"{PFX}Z1")
    assert [(c.id, cov) for c, cov in got] == [(f"{PFX}A", "edge")]
