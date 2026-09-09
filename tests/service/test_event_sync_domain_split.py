"""이벤트 이중화 — 한 지점에서 침수·교통 이벤트가 각각 생긴다
(flood/traffic 도메인 분리 확정사항 ②, 2026-08-21).

## 왜 이 시험이 있나

예전에는 ``_sync_flood`` 하나가 침수와 교통을 **한 이벤트로 합쳐** 올렸고,
등급마저 교통·기상 판정(`decision.level`)을 그대로 썼다. 관제요원이 화면에서
「이게 침수 때문인지 정체 때문인지」 구분할 수 없었다.

곁가지로 **detail 이 한 번도 채워진 적이 없던 결함**도 함께 잡는다 —
조회 키(`rain_mm`/`water_ratio`/`vehicles`)가 실제 스냅샷 키
(`rain_mm_h`/`water_area_ratio`/`n_vehicles`)와 달랐다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete

from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, Event, TrafficObservation
from tot_dashboard.core.roles import Domain
from tot_dashboard.service import event_sync as ES

PFX = "TEST-ESD-"


class _Store:
    def __init__(self, snaps):
        self._snaps = snaps

    def all(self):
        return self._snaps


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
        # ★ 2026-08-28 — traffic_history 잔여 행 정리. 이 파일의 여러 시험이
        # 같은 block_id(f"{PFX}A")로 _sync_traffic()을 반복 호출해 관측
        # 이력이 쌓인다. 안 지우면 "이력이 없어야 한다"를 확인하는 시험이
        # 다른 시험이 남긴 행 때문에 거짓 실패한다(2026-08-28 발견).
        db.execute(sa_delete(TrafficObservation).where(
            TrafficObservation.camera_id.like(f"{PFX}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


def _snap(**over):
    snap = {
        "block_id": f"{PFX}A", "name": "시험지점",
        # 교통 쪽
        "level": "경계", "rain_mm_h": 22.5, "intensity": "강함",
        "n_vehicles": 7, "mean_speed_kmh": 8.3,
        # 침수 쪽
        "water_available": True, "flood_risk_grade": 5,
        "flood_risk_score": 88.0, "water_area_ratio": 0.31,
        "vehicles_tire_in_water": 2, "persons_in_danger": 1,
    }
    snap.update(over)
    return snap


def _events_by_domain(db, block_id: str) -> dict[str, Event]:
    rows = db.query(Event).filter(Event.block_id == block_id).all()
    return {r.domain: r for r in rows}


def test_한_지점에서_침수와_교통_이벤트가_각각_생긴다():
    """★ 확정사항 ② — 혼합 상황에서 두 도메인이 각자 대응할 수 있어야 한다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap()])
        n = ES._sync_traffic(db, store) + ES._sync_flood(db, store)
        db.commit()

        assert n == 2
        rows = _events_by_domain(db, f"{PFX}A")
        assert set(rows) == {Domain.FLOOD.value, Domain.TRAFFIC.value}
    finally:
        db.close()


def test_두_이벤트의_등급이_서로_다른_근거에서_온다():
    """교통은 decision.level, 침수는 flood_risk_grade 에서 온다 — 예전에는
    둘 다 decision.level 이었다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        # 교통은 「주의」인데 침수는 5등급(심각)인 상황
        store = _Store([_snap(level="주의", flood_risk_grade=5)])
        ES._sync_traffic(db, store)
        ES._sync_flood(db, store)
        db.commit()

        rows = _events_by_domain(db, f"{PFX}A")
        assert rows[Domain.TRAFFIC.value].level == "주의"
        assert rows[Domain.FLOOD.value].level == "심각"
    finally:
        db.close()


def test_event_type이_도메인별로_나뉜다():
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap()])
        ES._sync_traffic(db, store)
        ES._sync_flood(db, store)
        db.commit()

        rows = _events_by_domain(db, f"{PFX}A")
        assert rows[Domain.TRAFFIC.value].event_type == "교통위험"
        assert rows[Domain.FLOOD.value].event_type == "침수"
    finally:
        db.close()


def test_물_세그멘테이션이_안_돌면_침수_이벤트를_안_만든다():
    """★ 「탐지 0건」과 「보지 못했다」는 다르다 — 안 본 것을 「이상 없음」
    등급으로 남기면 안 된다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap(water_available=False)])
        assert ES._sync_flood(db, store) == 0
        assert ES._sync_traffic(db, store) == 1  # 교통은 그대로 생긴다
        db.commit()

        rows = _events_by_domain(db, f"{PFX}A")
        assert set(rows) == {Domain.TRAFFIC.value}
    finally:
        db.close()


def test_detail이_실제로_채워진다():
    """★ 회귀 — 예전에는 조회 키가 스냅샷 키와 달라 detail 이 늘 비어 있었다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap()])
        ES._sync_traffic(db, store)
        ES._sync_flood(db, store)
        db.commit()

        rows = _events_by_domain(db, f"{PFX}A")
        tdetail = rows[Domain.TRAFFIC.value].detail or {}
        fdetail = rows[Domain.FLOOD.value].detail or {}
        assert tdetail.get("강수(mm/h)") == 22.5
        assert tdetail.get("차량 수") == 7
        assert fdetail.get("물 비율") == 0.31
        assert fdetail.get("침수 위험도") == 88.0
        # 침수 detail 에 교통 지표가 섞이지 않는다.
        assert "강수(mm/h)" not in fdetail
    finally:
        db.close()


@pytest.mark.parametrize("grade,expected", [
    (1, "관심"), (2, "관심"), (3, "주의"), (4, "경계"), (5, "심각")])
def test_침수_등급_대응표(grade, expected):
    """알림 기준(min_grade=4)과 「경계」에서 맞물리는지 확인한다."""
    assert ES._FLOOD_GRADE_LEVEL[grade] == expected


# --- hazard_type_code 배관 (2026-08-26) --------------------------------------
#
# 예전에는 record_detection() 에 hazard_type_code 를 아예 넘기지 않아,
# 위험유형 어휘 4종(강우정체·정지차량·대기열지연·통행불가)이 한 번도
# 채워진 적이 없었다(docs/202608260842/). risk_code(TWR_*)를 유형 어휘로
# 옮기는 배관이 실제로 동작하는지 확인한다.

def test_TWR판정코드가_위험유형으로_채워진다():
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap(risk_code="TWR_SEVERE_CONGESTION")])
        ES._sync_traffic(db, store)
        db.commit()

        rows = _events_by_domain(db, f"{PFX}A")
        assert rows[Domain.TRAFFIC.value].hazard_type_code == "traffic_stalled_vehicle"
    finally:
        db.close()


def test_TWR판정코드가_detail에도_남는다():
    """예전에는 TWR_* 가 이벤트 어디에도 저장되지 않아 사후 추적이
    불가능했다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap(risk_code="TWR_RAIN_CONGESTION")])
        ES._sync_traffic(db, store)
        db.commit()

        rows = _events_by_domain(db, f"{PFX}A")
        assert rows[Domain.TRAFFIC.value].detail.get("판정코드") == "TWR_RAIN_CONGESTION"
    finally:
        db.close()


def test_판정코드가_없으면_대분류로_떨어진다():
    """risk_code 가 스냅샷에 없거나 모르는 값이면 예전처럼 대분류
    "traffic" 로 정규화된다 — 예외를 내지 않는다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap(risk_code="")])
        ES._sync_traffic(db, store)
        db.commit()

        rows = _events_by_domain(db, f"{PFX}A")
        assert rows[Domain.TRAFFIC.value].hazard_type_code == "traffic"
    finally:
        db.close()


# --- 돌발상황(보행자 등) 배관 (2026-08-26, incident_events 연동) ------------

def test_돌발상황은_TWR판정과_별도_이벤트가_된다():
    """강우정체(TWR)와 보행자 도로 진입이 같은 카메라에서 동시에 잡혀도
    서로 덮어쓰면 안 된다 — split_by_hazard_type=True 로 나뉜다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap(risk_code="TWR_RAIN_CONGESTION", incidents=[
            {"code": "TIN_PEDESTRIAN", "label": "보행자 도로 진입",
             "level": "주의", "confidence": 0.8,
             "evidence_text": "도로 ROI 안 보행자 연속 관측 3초",
             "hazard_type_code": "traffic_pedestrian"},
        ])])
        n = ES._sync_traffic(db, store)
        db.commit()

        assert n == 2  # TWR 이벤트 1건 + 돌발상황 1건
        rows = db.query(Event).filter(
            Event.block_id == f"{PFX}A", Event.domain == Domain.TRAFFIC.value
        ).all()
        by_hazard = {r.hazard_type_code: r for r in rows}
        assert set(by_hazard) == {"traffic_rain_congestion", "traffic_pedestrian"}
        ped = by_hazard["traffic_pedestrian"]
        assert ped.level == "주의"
        assert ped.event_type == "보행자 도로 진입"
        assert ped.detail["근거"] == "도로 ROI 안 보행자 연속 관측 3초"
    finally:
        db.close()


def test_돌발상황이_없으면_추가_이벤트가_안_생긴다():
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap(risk_code="TWR_RAIN_CONGESTION", incidents=[])])
        n = ES._sync_traffic(db, store)
        db.commit()
        assert n == 1
    finally:
        db.close()


# --- 교통위험 미지정 카메라 게이트 (2026-08-28 실사용 점검 중 발견) ---------
#
# 침수만 상시로 켠 카메라(교통위험 미지정)는 실제로 교통 판정을 도는 것이
# 아니라 runner.py의 중립값(risk_code="")이 스냅샷에 실린다. 이 값 자체를
# 이력·이벤트로 남기면 「판정을 안 했다」가 아니라 「판정했는데 정상이었다」
# 로 보여 water_available 게이트(위 test_물_세그멘테이션이_안_돌면...)가
# 막으려던 것과 같은 함정에 빠진다.

def test_교통위험_미지정_카메라는_교통_이벤트를_만들지_않는다():
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        store = _Store([_snap(traffic_enabled=False, risk_code="", level="관심")])
        assert ES._sync_traffic(db, store) == 0
        db.commit()
        rows = _events_by_domain(db, f"{PFX}A")
        assert Domain.TRAFFIC.value not in rows
    finally:
        db.close()


def test_교통위험_미지정_카메라는_교통_이력도_안_남긴다():
    """★ 회귀 — 이 게이트가 없을 때는 침수만 상시인 카메라도 매 20초
    「정상」 교통 관측 이력이 쌓이고 있었다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        from tot_dashboard.core import traffic_history as TH
        store = _Store([_snap(traffic_enabled=False, risk_code="", level="관심")])
        ES._sync_traffic(db, store)
        db.commit()
        assert TH.recent(db, f"{PFX}A") == [], (
            "교통위험 미지정 카메라에 교통 관측 이력이 남았다")
    finally:
        db.close()


def test_traffic_enabled_키가_없으면_예전처럼_동작한다():
    """스냅샷에 traffic_enabled 자체가 없는(예전 버전) 경우 — 안전한
    기본값(True)으로 떨어져 기존 카메라의 이벤트 생성이 깨지면 안 된다."""
    db = get_session()
    try:
        db.add(Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0))
        db.commit()
        snap = _snap()
        assert "traffic_enabled" not in snap
        store = _Store([snap])
        assert ES._sync_traffic(db, store) == 1
        db.commit()
    finally:
        db.close()
