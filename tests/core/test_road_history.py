"""노면 점검 이력의 영구 보관 (core/road_history.py).

지켜야 할 것.

* **DB가 죽어도 관제가 멈추지 않는다** — 저장 실패는 삼키고, 조회 실패는
  ``None`` 을 돌려 호출자가 메모리로 내려가게 한다
* **``None`` 과 빈 목록을 섞지 않는다** — 빈 목록은 「관측한 적 없음」,
  ``None`` 은 「DB를 못 읽음」이다. 섞으면 폴백 판단이 무너진다
* **지점당 보관 상한** — 파기 정책이 정해지기 전까지 DB가 부풀지 않게 한다
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tot_dashboard.core import road_history as RH

pytestmark = pytest.mark.usefixtures("db_schema")


CAM = "TEST-HIST-CAM"


def _entry(n=0, *, failed=False, source="continuous", when=None):
    return {
        "analyzed_at": (when or datetime.now(timezone.utc)).isoformat(),
        "defect_count": n,
        "grade": None if failed else (3 if n else 1),
        "frames_analyzed": 0 if failed else 5,
        "failed": failed,
        "source": source,
    }


@pytest.fixture(autouse=True)
def clean():
    RH.reset_cache()
    _purge()
    yield
    _purge()
    RH.reset_cache()


def _purge():
    try:
        from tot_dashboard.core.db import get_session
        from tot_dashboard.core.models import RoadInspection
        db = get_session()
        try:
            db.query(RoadInspection).filter(
                RoadInspection.camera_id.like("TEST-HIST%")).delete(
                    synchronize_session=False)
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


# --- 저장·조회 ---------------------------------------------------------------

def test_관측을_남기고_다시_읽는다():
    assert RH.save(CAM, "시험지점", _entry(2)) is True
    rows = RH.recent(CAM, 10)
    assert rows is not None and len(rows) == 1
    assert rows[0]["defect_count"] == 2
    assert rows[0]["source"] == "continuous"


def test_오래된_것부터_돌려준다():
    """호출부(road/results.history)의 규약이다. 뒤집히면 추이 막대가 거꾸로 간다."""
    base = datetime.now(timezone.utc)
    for i in range(3):
        RH.save(CAM, "시험지점", _entry(i, when=base + timedelta(minutes=i)))
    rows = RH.recent(CAM, 10)
    assert [r["defect_count"] for r in rows] == [0, 1, 2]


def test_최근_N건만_가져온다():
    base = datetime.now(timezone.utc)
    for i in range(8):
        RH.save(CAM, "시험지점", _entry(i, when=base + timedelta(minutes=i)))
    rows = RH.recent(CAM, 3)
    # 최근 3건이되 오래된 것부터
    assert [r["defect_count"] for r in rows] == [5, 6, 7]


def test_분석_실패도_그대로_남긴다():
    """「봤는데 없었다」와 「못 봤다」는 다르다. 이벤트로는 남지 않는 정보다."""
    RH.save(CAM, "시험지점", _entry(0, failed=True))
    r = RH.recent(CAM, 5)[0]
    assert r["failed"] is True
    assert r["frames_analyzed"] == 0
    assert r["grade"] is None      # 0 으로 두면 「정상」으로 읽힌다


def test_출처를_구분해_남긴다():
    """상시 순회 결과와 사람이 눌러 돌린 결과를 구분해야 값이 튀는 이유를 안다."""
    RH.save(CAM, "시험지점", _entry(1, source="continuous"))
    RH.save(CAM, "시험지점", _entry(1, source="focus"))
    RH.save(CAM, "시험지점", _entry(1, source="manual"))
    assert {r["source"] for r in RH.recent(CAM, 9)} == {
        "continuous", "focus", "manual"}


def test_지점이_다르면_섞이지_않는다():
    RH.save(CAM, "가", _entry(1))
    RH.save("TEST-HIST-OTHER", "나", _entry(2))
    assert [r["defect_count"] for r in RH.recent(CAM, 9)] == [1]


def test_관측이_없으면_빈_목록이지_None이_아니다():
    """빈 목록은 「관측한 적 없음」, None 은 「DB를 못 읽음」이다."""
    assert RH.recent("TEST-HIST-NEVER", 5) == []


# --- 보관 상한 ---------------------------------------------------------------

def test_지점당_상한을_넘으면_오래된_것부터_지운다(monkeypatch):
    """파기 정책이 정해지기 전까지 DB가 무한히 부풀지 않게 한다."""
    monkeypatch.setattr(RH, "KEEP_PER_CAMERA", 5)
    monkeypatch.setattr(RH, "TRIM_SLACK", 2)
    base = datetime.now(timezone.utc)
    for i in range(12):
        RH.save(CAM, "시험지점", _entry(i, when=base + timedelta(minutes=i)))
    rows = RH.recent(CAM, 100)
    assert len(rows) <= 5 + 2
    # 남은 것은 최신 쪽이어야 한다
    assert rows[-1]["defect_count"] == 11


# --- 견고성 -----------------------------------------------------------------

def test_DB를_쓸_수_없으면_조회가_None을_돌려준다(monkeypatch):
    """호출자가 메모리 이력으로 내려갈 수 있어야 한다."""
    RH.reset_cache()
    monkeypatch.setattr(RH, "available", lambda: False)
    assert RH.recent(CAM, 5) is None


def test_DB를_쓸_수_없으면_저장은_조용히_실패한다(monkeypatch):
    RH.reset_cache()
    monkeypatch.setattr(RH, "available", lambda: False)
    assert RH.save(CAM, "시험지점", _entry(1)) is False


def test_저장_중_오류가_나도_예외를_올리지_않는다(monkeypatch):
    """관제 분석이 DB 때문에 멈추면 안 된다."""
    def _boom(*a, **k):
        raise RuntimeError("DB 오류")
    monkeypatch.setattr(RH, "_session", _boom)
    RH.reset_cache()
    monkeypatch.setattr(RH, "available", lambda: True)
    assert RH.save(CAM, "시험지점", _entry(1)) is False


def test_조회_중_오류가_나도_None으로_내려간다(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("DB 오류")
    monkeypatch.setattr(RH, "_session", _boom)
    RH.reset_cache()
    monkeypatch.setattr(RH, "available", lambda: True)
    assert RH.recent(CAM, 5) is None


def test_시각_형식이_깨져도_저장은_된다():
    """이력이 한 건 어긋나는 것보다 통째로 못 남기는 것이 나쁘다."""
    e = _entry(1)
    e["analyzed_at"] = "어제쯤"
    assert RH.save(CAM, "시험지점", e) is True
    assert len(RH.recent(CAM, 5)) == 1
