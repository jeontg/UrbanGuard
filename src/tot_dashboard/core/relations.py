"""관계 조회와 자동 생성 — Urban Ontology 1단계.

설계는 ``docs/202608181432/relation_model_design.md`` 에 있다.

**왜 GraphDB 를 안 쓰는가.** 경남 제안요청서 규모가 4,500채널이다. 채널당
링크를 4개로 잡아도 18,000행이라, 인덱스가 걸린 재귀 CTE 로 밀리초 단위다.
별도 DBMS 를 폐쇄망에 하나 더 설치하는 비용이 이득보다 크다
(``docs/202608181432/tech_adoption_judgment.md`` 5절).

**추측이 아니라 실측이 말할 때 바꾼다.** :func:`neighbors` 가 실제로 느려지면
그때 다시 판단한다.
"""
from __future__ import annotations

import math

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import Camera, CameraLink, CameraSensor, CameraZone, Sensor, Zone

# 인접 자동 생성 기준. 500m 는 **가정이며 실측이 아니다** — 지자체 CCTV 간격이
# 지역마다 다르다. 화면에서 조정할 수 있게 인자로 받는다.
DEFAULT_RADIUS_M = 500

# 재귀 탐색 기본 깊이. 2홉이면 「옆의 옆」까지다. 더 늘리면 화면에 띄울 수 없을
# 만큼 카메라가 딸려 온다.
DEFAULT_MAX_HOP = 2

# 물길을 따라가는 관계. 인접과 달리 **방향이 있다.**
FLOW_KINDS = ("upstream", "downstream")
ADJACENT = "adjacent"

_EARTH_R_M = 6_371_000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 사이 거리(m).

    PostGIS 가 설치본에 없어 순수 계산으로 한다. 39지점 전수 비교가 7.2ms 로
    측정된 방식과 같다(``docs/202608171820``).
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    """1 → 2 방위각(0~359, 북이 0)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return int(round(math.degrees(math.atan2(y, x)))) % 360


# --- 조회 -------------------------------------------------------------------


def neighbors(db: Session, camera_id: str, *, max_hop: int = DEFAULT_MAX_HOP,
              kinds: tuple[str, ...] = (ADJACENT,)) -> list[tuple[str, int]]:
    """인접 카메라를 N홉까지 찾는다. ``[(camera_id, depth), …]``.

    재귀 CTE 한 방이다. GraphDB 없이 되는 이유가 이 함수 하나에 있다.

    ``max_hop`` 을 반드시 거는 이유 — 링크가 순환하면 재귀가 끝나지 않는다.
    ``UNION`` 이 중복은 걸러 주지만 깊이는 걸러 주지 못한다.

    ⚠️ **출발 지점을 결과에서 뺀다.** 인접은 양방향이라 A→B→A 로 두 홉 만에
    자기 자신이 되돌아온다. 시험이 이걸 잡았다 —
    ``test_neighbors_two_hop_reaches_c``. ``depth > 0`` 만으로는 못 거른다.
    """
    if max_hop < 1 or not kinds:
        return []
    sql = text("""
        WITH RECURSIVE nearby(camera_id, depth) AS (
            SELECT CAST(:start AS varchar), 0
          UNION
            SELECT l.to_camera_id, n.depth + 1
              FROM camera_links l
              JOIN nearby n ON l.from_camera_id = n.camera_id
             WHERE n.depth < :max_hop
               AND l.kind = ANY(:kinds)
        )
        SELECT camera_id, MIN(depth) AS depth
          FROM nearby
         WHERE depth > 0
           AND camera_id <> CAST(:start AS varchar)
         GROUP BY camera_id
         ORDER BY depth, camera_id
    """)
    rows = db.execute(sql, {"start": camera_id, "max_hop": max_hop,
                            "kinds": list(kinds)}).all()
    return [(r[0], r[1]) for r in rows]


def downstream_of(db: Session, camera_id: str, *,
                  max_hop: int = DEFAULT_MAX_HOP) -> list[tuple[str, int]]:
    """이 지점의 하류 지점들.

    **상류가 차오르면 하류를 미리 경고**하는 데 쓴다. 지금 우리가 못 하는
    일이고, 경남 제안요청서 기대효과의 「연속 추적」이 실제로 요구하는 것이다.
    """
    return neighbors(db, camera_id, max_hop=max_hop, kinds=("downstream",))


def sensors_for(db: Session, camera_id: str, *,
                primary_only: bool = False) -> list[tuple[Sensor, str]]:
    """카메라에 붙은 센서. ``[(Sensor, role), …]``. 대표 센서가 앞에 온다.

    근거는 경남 SFR-005 「센서 이벤트 발생 시 연관된 카메라 영상을 즉시 호출」의
    반대 방향이다 — 카메라를 보다가 센서값을 확인하는 경로.
    """
    q = (select(Sensor, CameraSensor.role)
         .join(CameraSensor, CameraSensor.sensor_id == Sensor.id)
         .where(CameraSensor.camera_id == camera_id))
    if primary_only:
        q = q.where(CameraSensor.role == "primary")
    rows = db.execute(q).all()
    # 대표를 앞으로. role 문자열 정렬은 우연히 primary < reference 지만
    # 우연에 기대지 않는다.
    return sorted(((s, r) for s, r in rows), key=lambda t: (t[1] != "primary",
                                                            t[0].id))


def cameras_for_sensor(db: Session, sensor_id: str) -> list[Camera]:
    """센서에 걸린 카메라들 — **N:1 이라 여럿일 수 있다**(경남 SFR-005)."""
    q = (select(Camera)
         .join(CameraSensor, CameraSensor.camera_id == Camera.id)
         .where(CameraSensor.sensor_id == sensor_id)
         .order_by(Camera.id))
    return list(db.execute(q).scalars())


def zones_under(db: Session, path_prefix: str) -> list[Zone]:
    """경로 아래 구역 전부. ``kr.gyeongnam`` → 경남 하위 전부.

    ``ltree`` 없이 ``LIKE`` 로 한다. 자기 자신도 포함한다 — 「창원시 아래」를
    물으면 창원시도 답에 들어가는 것이 자연스럽다.
    """
    p = (path_prefix or "").strip().strip(".")
    if not p:
        return []
    # ``%`` 와 ``_`` 는 LIKE 메타문자다. 구역 경로에 들어갈 일이 없지만,
    # 사용자 입력이 여기까지 온다면 막아야 한다.
    esc = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    q = (select(Zone)
         .where((Zone.path == p) | (Zone.path.like(f"{esc}.%", escape="\\")))
         .order_by(Zone.path, Zone.id))
    return list(db.execute(q).scalars())


def nearby_cameras(db: Session, camera_id: str, *,
                   max_hop: int = 1, limit: int = 8) -> list[dict]:
    """이벤트 상세(S-03)에 띄울 주변 지점.

    인접에 더해 **하류까지 같이** 준다. 상류에서 사건이 났으면 하류가 다음
    차례라, 관제요원이 함께 봐야 하는 지점이다.

    ``limit`` 을 거는 이유 — 도심은 반경 500m 안에 지점이 열 개 넘게 들어간다.
    전부 띄우면 화면이 목록으로 덮인다.
    """
    seen: dict[str, dict] = {}
    # 하류를 먼저 넣는다 — 같은 지점이 양쪽에 걸리면 **하류라는 사실이 더
    # 중요하다.** 인접은 「옆에 있다」지만 하류는 「다음에 잠긴다」다.
    for kind in ("downstream", ADJACENT):
        for cid, depth in neighbors(db, camera_id, max_hop=max_hop,
                                    kinds=(kind,)):
            if cid in seen:
                continue
            seen[cid] = {"id": cid, "kind": kind, "depth": depth}
    if not seen:
        return []
    rows = db.execute(select(Camera).where(Camera.id.in_(list(seen)))).scalars()
    by_id = {c.id: c for c in rows}
    out = []
    for cid, info in seen.items():
        cam = by_id.get(cid)
        if cam is None:      # 링크만 남고 카메라가 지워진 경우
            continue
        out.append({**info, "name": cam.name, "lat": cam.lat, "lng": cam.lng})
    # 하류 먼저, 그다음 가까운 순.
    out.sort(key=lambda d: (d["kind"] != "downstream", d["depth"], d["id"]))
    return out[:limit]


def downstream_warnings(db: Session, *, min_rank: int = 3,
                        max_hop: int = DEFAULT_MAX_HOP) -> list[dict]:
    """★ 선행 경고 — 상류에서 사건이 열려 있으면 하류를 미리 알린다.

    **경쟁사에 없는 기능이고, 관계 모델이 여는 값어치의 핵심이다.**
    경남 제안요청서 기대효과의 「시·군 경계를 넘나드는 재난을 연속 추적」이
    실제로 요구하는 것이 이것이다.

    ``min_rank`` 는 등급 문턱이다. 기본 3 은 ``risk_levels`` 의 seq 기준으로
    「경계」에 해당한다. ⚠️ **이름이 아니라 seq 로 비교한다** — 기관이 등급
    이름을 바꿔도 문턱이 그대로 맞는다.

    ⚠️ 하류에 **이미 자기 이벤트가 열려 있으면 경고하지 않는다.** 이미 아는
    일을 다시 알리면 경보 피로만 쌓인다.
    """
    from . import events as EV                       # 순환 임포트 회피
    from .models import Event
    from . import vocabulary as V

    # ⚠️ 2026-09-02 — `!= "closed"` 대신 `EV.ACTIVE`(open+progress)
    # 화이트리스트를 쓴다. S-01/S-02와 같은 정의를 쓰지 않으면, 새 상태
    # (예: 재탐지 없이 오래 방치돼 자동 보류된 이벤트)가 생겼을 때 이
    # 함수만 계속 "하류에 이미 자기 이벤트가 열려 있다"고 착각해
    # 선행경고를 계속 억제하게 된다.
    open_evs = list(db.execute(
        select(Event).where(Event.status.in_(EV.ACTIVE))).scalars())
    if not open_evs:
        return []
    busy = {e.block_id for e in open_evs if e.block_id}

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for ev in open_evs:
        if not ev.block_id:
            continue
        if V.rank(db, ev.level) < min_rank:
            continue
        for cid, depth in downstream_of(db, ev.block_id, max_hop=max_hop):
            if cid in busy:
                continue        # 하류도 이미 자기 사건이 있다
            key = (ev.block_id, cid)
            if key in seen:
                continue
            seen.add(key)
            cam = db.get(Camera, cid)
            out.append({
                "source_id": ev.block_id,
                "source_name": ev.place_name or ev.block_id,
                "source_level": ev.level,
                "domain": ev.domain,
                "event_id": ev.id,
                "target_id": cid,
                "target_name": cam.name if cam else cid,
                "depth": depth,
            })
    out.sort(key=lambda d: (d["depth"], d["target_id"]))
    return out


def cameras_in_zone(db: Session, zone_id: str) -> list[tuple[Camera, str]]:
    """구역에 속한 카메라. ``[(Camera, coverage), …]``."""
    q = (select(Camera, CameraZone.coverage)
         .join(CameraZone, CameraZone.camera_id == Camera.id)
         .where(CameraZone.zone_id == zone_id)
         .order_by(Camera.id))
    return [(c, cov) for c, cov in db.execute(q).all()]


# --- 자동 생성 --------------------------------------------------------------


def set_flow(db: Session, upper_id: str, lower_id: str) -> list[str]:
    """상류 → 하류 관계를 지정한다(사람이 하는 일).

    ⚠️ **좌표로는 알 수 없다.** 지형과 배수 계통을 봐야 알 수 있어서
    사람이 넣는다. 그래서 ``auto=False`` 로 저장하고, 자동 생성이
    이 행을 건드리지 않는다.

    반대 방향(``upstream``)도 함께 넣는다 — 하류 지점에서 「내 위가 어디냐」를
    물을 수 있어야 한다.
    """
    errs: list[str] = []
    if upper_id == lower_id:
        return ["같은 지점을 상·하류로 지정할 수 없습니다."]
    for cid in (upper_id, lower_id):
        if db.get(Camera, cid) is None:
            errs.append(f"지점을 찾을 수 없습니다: {cid}")
    if errs:
        return errs
    # 이미 반대로 지정돼 있으면 물이 두 방향으로 흐르는 셈이 된다.
    reverse = db.query(CameraLink).filter_by(
        from_camera_id=lower_id, to_camera_id=upper_id,
        kind="downstream").first()
    if reverse is not None:
        return ["이미 반대 방향으로 지정돼 있습니다. 먼저 해제하세요."]

    for frm, to, kind in ((upper_id, lower_id, "downstream"),
                          (lower_id, upper_id, "upstream")):
        got = db.query(CameraLink).filter_by(
            from_camera_id=frm, to_camera_id=to, kind=kind).first()
        if got is None:
            db.add(CameraLink(from_camera_id=frm, to_camera_id=to, kind=kind,
                              auto=False))
    db.flush()
    return []


def clear_flow(db: Session, upper_id: str, lower_id: str) -> int:
    """상·하류 지정을 양방향으로 지운다."""
    n = db.query(CameraLink).filter(
        CameraLink.kind.in_(FLOW_KINDS),
        ((CameraLink.from_camera_id == upper_id)
         & (CameraLink.to_camera_id == lower_id))
        | ((CameraLink.from_camera_id == lower_id)
           & (CameraLink.to_camera_id == upper_id))).delete(
               synchronize_session=False)
    db.flush()
    return n


def adjacency_count(db: Session) -> int:
    """자동 생성된 인접 링크 수. 화면에 「몇 건이 걸려 있는지」를 보여 준다."""
    return db.query(CameraLink).filter(
        CameraLink.kind == ADJACENT, CameraLink.auto.is_(True)).count()


def flow_pairs(db: Session) -> list[dict]:
    """지정된 상·하류 쌍 목록. 화면에 그대로 띄운다."""
    rows = db.execute(
        select(CameraLink).where(CameraLink.kind == "downstream")
        .order_by(CameraLink.from_camera_id)).scalars()
    names = {c.id: c.name for c in db.execute(select(Camera)).scalars()}
    return [{"upper_id": r.from_camera_id,
             "upper_name": names.get(r.from_camera_id, r.from_camera_id),
             "lower_id": r.to_camera_id,
             "lower_name": names.get(r.to_camera_id, r.to_camera_id)}
            for r in rows]


def build_adjacency(db: Session, *, radius_m: int = DEFAULT_RADIUS_M,
                    dry_run: bool = False) -> dict:
    """좌표로 ``adjacent`` 링크를 만든다.

    ⚠️ **``auto=True`` 행만 지우고 다시 만든다.** 사람이 넣은 상·하류 관계까지
    지우면 복구할 방법이 없다 — 상·하류는 좌표만으로 알 수 없어서 사람이 넣은
    것이다.

    양방향으로 만든다. A 가 B 의 이웃이면 B 도 A 의 이웃이다. 한쪽만 만들면
    :func:`neighbors` 가 방향에 따라 다른 답을 낸다.

    ``dry_run`` 이면 세기만 하고 쓰지 않는다 — 반경을 바꿔 볼 때 쓴다.
    """
    cams = [c for c in db.execute(select(Camera)).scalars()
            if c.lat is not None and c.lng is not None]
    pairs: list[tuple[str, str, int, int]] = []
    for i, a in enumerate(cams):
        for b in cams[i + 1:]:
            d = haversine_m(a.lat, a.lng, b.lat, b.lng)
            if d > radius_m:
                continue
            # ⚠️ 좌표가 같은 지점이 실제로 있다(광복로/광복로1 이 범내골교차로와
            # 같은 좌표다 — 데이터 결함, docs/202608171820). 거리 0을 이웃으로
            # 인정하면 서로 다른 지점이 한 덩어리로 묶인다. 다만 **여기서
            # 고치지 않는다** — 좌표를 지어내지 않는 것이 원칙이다.
            m = int(round(d))
            pairs.append((a.id, b.id, m, bearing_deg(a.lat, a.lng, b.lat, b.lng)))
            pairs.append((b.id, a.id, m, bearing_deg(b.lat, b.lng, a.lat, a.lng)))

    zero = sum(1 for _, _, m, _ in pairs if m == 0) // 2
    if dry_run:
        return {"cameras": len(cams), "links": len(pairs), "removed": 0,
                "radius_m": radius_m, "zero_distance_pairs": zero,
                "dry_run": True}

    removed = db.query(CameraLink).filter(
        CameraLink.auto.is_(True), CameraLink.kind == ADJACENT).delete(
            synchronize_session=False)
    for frm, to, dist, brg in pairs:
        db.add(CameraLink(from_camera_id=frm, to_camera_id=to, kind=ADJACENT,
                          distance_m=dist, bearing_deg=brg, auto=True))
    db.flush()
    return {"cameras": len(cams), "links": len(pairs), "removed": removed,
            "radius_m": radius_m, "zero_distance_pairs": zero,
            "dry_run": False}
