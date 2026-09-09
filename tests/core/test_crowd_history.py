"""인파 관측 이력 (core/crowd_history.py).

지켜야 할 것.

* **평온했던 관측도 남는다** — 이벤트는 위험행동이 잡혔을 때만 생긴다.
  기준선을 만들려면 「봤는데 아무 일 없었다」가 있어야 한다
* **「못 봤다」와 「사람이 없었다」를 구분한다** — 섞으면 기준선이 망가진다
* **surge 기본값은 1.0** — 0 이면 「속도가 0배」라는 뜻이 되어 통계가 뒤집힌다
* **관측이 없으면 빈 기준선을 준다** — 0 을 주면 「평소에 아무도 없다」로 읽혀
  지금 인원이 전부 급증으로 보인다
* **보존기간이 지나면 지운다** — 개인 식별 정보는 없지만 무한히 쌓지 않는다
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tot_dashboard.core import crowd_history as CH
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import CrowdObservation
from sqlalchemy import delete as sa_delete

CAM = "TEST-CROWD-HIST"


def _purge_all(db):
    db.execute(sa_delete(CrowdObservation)
               .where(CrowdObservation.camera_id.like("TEST-CROWD%")))
    db.commit()


@pytest.fixture
def db(db_schema):
    s = get_session()
    _purge_all(s)
    yield s
    _purge_all(s)
    s.close()


def _snap(**kw) -> dict:
    base = {"person_count": 10, "density_index": 0.4, "mean_speed": 12.0,
            "trajectory_variance": 0.3, "surge": 1.2, "divergence": 2.5,
            "risk_code": "NORMAL", "risk_score": 0.21, "severity": 0,
            "drivers": [], "source": "detector"}
    base.update(kw)
    return base


# --- 기록 --------------------------------------------------------------------
def test_평온한_관측도_남는다(db):
    """이벤트가 없어도 남아야 기준선이 생긴다."""
    CH.record(db, camera_id=CAM, camera_name="시험지점", snapshot=_snap())
    db.commit()
    rows = CH.recent(db, CAM)
    assert len(rows) == 1
    assert rows[0].risk_code == "NORMAL"
    assert rows[0].person_count == 10


def test_흐름_지표가_그대로_남는다(db):
    CH.record(db, camera_id=CAM,
              snapshot=_snap(surge=1.8, divergence=7.5,
                             trajectory_variance=0.62))
    db.commit()
    r = CH.recent(db, CAM)[0]
    assert r.surge == pytest.approx(1.8)
    assert r.divergence == pytest.approx(7.5)
    assert r.dispersion == pytest.approx(0.62)


def test_판정_근거를_문자열로_남긴다(db):
    """「왜 그때 경보가 떴나」를 되짚을 때 숫자만 남으면 답할 수 없다."""
    CH.record(db, camera_id=CAM, snapshot=_snap(drivers=["발산도UP", "속도급증"]))
    db.commit()
    assert "발산도UP" in CH.recent(db, CAM)[0].drivers


def test_surge_기본값은_평상시다(db):
    """0 이면 「속도가 0배」라는 뜻이 되어 통계가 뒤집힌다."""
    CH.record(db, camera_id=CAM, snapshot={"person_count": 3})
    db.commit()
    assert CH.recent(db, CAM)[0].surge == pytest.approx(1.0)


def test_카메라_없으면_기록하지_않는다(db):
    assert CH.record(db, camera_id="", snapshot=_snap()) is None
    assert CH.record(db, camera_id="   ", snapshot=_snap()) is None


# --- 못 봤다 vs 사람이 없었다 ------------------------------------------------
def test_실패한_관측은_따로_표시된다(db):
    CH.record(db, camera_id=CAM, snapshot={}, failed=True)
    db.commit()
    assert CH.recent(db, CAM)[0].failed is True


def test_실패한_관측은_시계열에서_빠진다(db):
    """못 본 구간을 0 으로 채우면 예측이 「사람이 빠졌다」로 학습한다."""
    CH.record(db, camera_id=CAM, snapshot=_snap(person_count=12))
    CH.record(db, camera_id=CAM, snapshot={}, failed=True)
    db.commit()
    rows = CH.series(db, CAM, hours=24)
    assert len(rows) == 1
    assert rows[0].person_count == 12


# --- 기준선 ------------------------------------------------------------------
def test_관측이_없으면_빈_기준선을_준다(db):
    """0 을 주면 지금 인원이 전부 급증으로 보인다."""
    assert CH.baseline(db, "TEST-CROWD-NOBODY") == {}


def test_기준선은_중앙값을_준다(db):
    for n in (2, 4, 6, 8, 100):     # 100 은 튀는 값
        CH.record(db, camera_id=CAM, snapshot=_snap(person_count=n))
    db.commit()
    b = CH.baseline(db, CAM, hours=24)
    assert b["samples"] == 5
    assert b["person_median"] == 6, "평균이면 튀는 값에 끌려간다"
    assert b["person_max"] == 100


# --- 시계열 ------------------------------------------------------------------
def test_시계열은_시간_오름차순이다(db):
    """예측 모델에 넣을 형태다."""
    for n in (1, 2, 3):
        CH.record(db, camera_id=CAM, snapshot=_snap(person_count=n))
    db.commit()
    rows = CH.series(db, CAM, hours=24)
    assert [r.person_count for r in rows] == [1, 2, 3]


def test_최근_조회는_내림차순이다(db):
    for n in (1, 2, 3):
        CH.record(db, camera_id=CAM, snapshot=_snap(person_count=n))
    db.commit()
    assert CH.recent(db, CAM)[0].person_count == 3


def test_다른_지점은_섞이지_않는다(db):
    CH.record(db, camera_id=CAM, snapshot=_snap(person_count=5))
    CH.record(db, camera_id="TEST-CROWD-OTHER", snapshot=_snap(person_count=99))
    db.commit()
    assert [r.person_count for r in CH.recent(db, CAM)] == [5]


# --- 보존기간 ----------------------------------------------------------------
def test_보존기간이_지난_것을_지운다(db):
    CH.record(db, camera_id=CAM, snapshot=_snap())
    old = CH.record(db, camera_id=CAM, snapshot=_snap())
    db.flush()
    old.observed_at = datetime.now(timezone.utc) - timedelta(days=500)
    db.commit()

    n = CH.purge(db, keep_days=400)
    db.commit()
    assert n == 1
    assert len(CH.recent(db, CAM)) == 1


def test_보존기간이_0이면_지우지_않는다(db):
    """사람이 정하지 않았는데 시스템이 이력을 지우기 시작하면 안 된다."""
    CH.record(db, camera_id=CAM, snapshot=_snap())
    db.commit()
    assert CH.purge(db, keep_days=0) == 0


# --- 실시간 관제 카드 (2026-08-27, 인파 상시 카메라별 모니터링 신설) --------

def test_관측이_없으면_미관측으로_정직하게_답한다(db):
    """★ road/results.py::summary와 같은 원칙 — 등급을 매기지 않는다."""
    s = CH.summary(db, "TEST-CROWD-NEVER-SEEN", "시험지점")
    assert s["observed"] is False
    assert s["severity"] is None
    assert s["age_sec"] is None


def test_최근_관측을_카드_형태로_돌려준다(db):
    CH.record(db, camera_id=CAM, camera_name="시험지점",
             snapshot=_snap(person_count=7, severity=2), source="detector")
    db.commit()
    s = CH.summary(db, CAM, "시험지점")
    assert s["observed"] is True
    assert s["failed"] is False
    assert s["person_count"] == 7
    assert s["severity"] == 2
    assert s["source"] == "detector"
    assert s["age_sec"] is not None and s["age_sec"] >= 0


def test_실패한_관측은_failed로_구분된다(db):
    CH.record(db, camera_id=CAM, camera_name="시험지점",
             snapshot=_snap(), failed=True)
    db.commit()
    s = CH.summary(db, CAM, "시험지점")
    assert s["observed"] is True
    assert s["failed"] is True


def test_age_seconds는_시각이_없으면_None이다():
    assert CH.age_seconds(None) is None


def test_age_seconds는_경과_시간을_초로_돌려준다():
    t = datetime.now(timezone.utc) - timedelta(seconds=30)
    age = CH.age_seconds(t)
    assert age is not None and 29 <= age <= 35
