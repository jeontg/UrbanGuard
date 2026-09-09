"""카메라별 「지금」 판정의 크로스 프로세스 조회 (core/live_state.py).

지켜야 할 것.

* **DB가 죽어도 판정이 멈추지 않는다** — 저장 실패는 삼키고, 조회 실패는
  ``None`` 을 돌려 호출자가 "관측 없음"으로 내려가게 한다
* **``None`` 과 빈 dict를 섞지 않는다** — 빈 dict는 「해당 카메라 관측
  없음」, ``None`` 은 「DB를 못 읽음」이다
* **행 하나만 계속 덮어쓴다** — 이력이 아니라 "지금" 하나만 필요하다
* **도메인이 다르면 섞이지 않는다** — 같은 카메라라도 침수·교통은 별개다
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import live_state as LS

pytestmark = pytest.mark.usefixtures("db_schema")

CAM = "TEST-LIVESTATE-CAM"


@pytest.fixture(autouse=True)
def clean():
    LS.reset_cache()
    _purge()
    yield
    _purge()
    LS.reset_cache()


def _purge():
    try:
        from tot_dashboard.core.db import get_session
        from tot_dashboard.core.models import LiveDetectionState
        db = get_session()
        try:
            db.query(LiveDetectionState).filter(
                LiveDetectionState.camera_id.like("TEST-LIVESTATE%")).delete(
                    synchronize_session=False)
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


# --- 저장·조회 ---------------------------------------------------------------

def test_저장하고_다시_읽는다():
    assert LS.upsert(CAM, "flood", level="경계", is_available=True) is True
    rows = LS.latest_by_camera([CAM], "flood")
    assert rows is not None
    assert rows[CAM]["level"] == "경계"
    assert rows[CAM]["available"] is True


def test_같은_카메라_도메인에_다시_쓰면_행이_늘지_않고_덮어쓴다():
    """이력이 아니다 — 지금 상태 하나만 있으면 된다."""
    LS.upsert(CAM, "flood", level="관심")
    LS.upsert(CAM, "flood", level="심각")
    rows = LS.latest_by_camera([CAM], "flood")
    assert rows[CAM]["level"] == "심각"

    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import LiveDetectionState
    from sqlalchemy import select, func
    db = get_session()
    try:
        n = db.scalar(select(func.count()).select_from(LiveDetectionState)
                      .where(LiveDetectionState.camera_id == CAM,
                             LiveDetectionState.domain == "flood"))
        assert n == 1
    finally:
        db.close()


def test_도메인이_다르면_섞이지_않는다():
    LS.upsert(CAM, "flood", level="심각")
    LS.upsert(CAM, "traffic", level="관심")
    flood_rows = LS.latest_by_camera([CAM], "flood")
    traffic_rows = LS.latest_by_camera([CAM], "traffic")
    assert flood_rows[CAM]["level"] == "심각"
    assert traffic_rows[CAM]["level"] == "관심"


def test_카메라가_다르면_섞이지_않는다():
    LS.upsert(CAM, "flood", level="심각")
    LS.upsert("TEST-LIVESTATE-OTHER", "flood", level="관심")
    rows = LS.latest_by_camera([CAM], "flood")
    assert list(rows.keys()) == [CAM]


def test_관측이_없으면_빈_dict이지_None이_아니다():
    """빈 dict는 「해당 카메라 관측 없음」, None 은 「DB를 못 읽음」이다."""
    assert LS.latest_by_camera(["TEST-LIVESTATE-NEVER"], "flood") == {}


def test_available_플래그로_판정_없는_카메라를_가릴_수_있다():
    """예: 재배포 장애로 이번 틱은 판정을 아예 안 돌린 경우."""
    LS.upsert(CAM, "traffic", level="", is_available=False)
    rows = LS.latest_by_camera([CAM], "traffic")
    assert rows[CAM]["available"] is False


# --- 견고성 -----------------------------------------------------------------

def test_DB를_쓸_수_없으면_조회가_None을_돌려준다(monkeypatch):
    LS.reset_cache()
    monkeypatch.setattr(LS, "available", lambda: False)
    assert LS.latest_by_camera([CAM], "flood") is None


def test_DB를_쓸_수_없으면_저장은_조용히_실패한다(monkeypatch):
    LS.reset_cache()
    monkeypatch.setattr(LS, "available", lambda: False)
    assert LS.upsert(CAM, "flood", level="심각") is False


def test_저장_중_오류가_나도_예외를_올리지_않는다(monkeypatch):
    """도메인 서비스의 판정 루프가 이 표 때문에 멈추면 안 된다."""
    def _boom(*a, **k):
        raise RuntimeError("DB 오류")
    monkeypatch.setattr(LS, "_session", _boom)
    LS.reset_cache()
    monkeypatch.setattr(LS, "available", lambda: True)
    assert LS.upsert(CAM, "flood", level="심각") is False


def test_조회_중_오류가_나도_None으로_내려간다(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("DB 오류")
    monkeypatch.setattr(LS, "_session", _boom)
    LS.reset_cache()
    monkeypatch.setattr(LS, "available", lambda: True)
    assert LS.latest_by_camera([CAM], "flood") is None


def test_camera_id나_domain이_비면_저장하지_않는다():
    assert LS.upsert("", "flood", level="심각") is False
    assert LS.upsert(CAM, "", level="심각") is False
