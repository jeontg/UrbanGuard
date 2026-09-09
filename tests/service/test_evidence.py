"""탐지 이벤트 증거 영상 (S-88).

**왜 있는 기능인가.** 지금까지 이벤트에는 숫자와 라벨만 남았다. 「무슨 일이
있었는가」를 보여 줄 자료가 없어, 사후 검토도 대외 보고도 근거가 없었다.

지켜야 할 것.

* **관리자가 등급을 고른다** — 영상은 개인정보라 「일단 다 남기고 보자」로
  두면 안 된다
* **고르지 않은 등급은 남기지 않는다**
* **이벤트로 올라오지 않는 등급은 골라도 안 남는다** — 화면이 그 사실을
  말해야 한다(임계값이 「주의」)
* **링 버퍼가 이벤트 「직전」을 담는다** — 탐지 후에 녹화를 시작하면 정작
  중요한 순간이 빠진다
* **내려받기·삭제는 감사 로그에 남는다** — 개인정보를 다루는 행위다
* **삭제는 파일까지 지운다** — DB만 지우면 주인 없는 영상이 디스크에 남는다
* **관제요원은 열람만** — 삭제는 되돌릴 수 없다
"""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from tot_dashboard.core import events as E
from tot_dashboard.core import evidence as EV
from tot_dashboard.core import roles as R
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (AppSetting, AuditLog, Event,
                                       EventAction, EventEvidence)
from tot_dashboard.service import evidence_capture
from tot_dashboard.service.main import app

CAM = "EVID-CAM"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def clean(db_schema, tmp_path, monkeypatch):
    # 시험이 실제 데이터 폴더를 건드리지 않게 한다.
    monkeypatch.setattr(EV, "EVIDENCE_DIR", tmp_path / "evidence")
    EV.reset()
    _purge()
    yield
    EV.reset()
    _purge()


def _purge():
    s = get_session()
    try:
        s.execute(sa_delete(EventEvidence))
        s.execute(sa_delete(EventAction))
        s.execute(sa_delete(Event))
        s.execute(sa_delete(AuditLog))
        s.execute(sa_delete(AppSetting))
        s.commit()
    finally:
        s.close()
    S.invalidate()


def _frame(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)


def _fill_ring(n: int = 6):
    """링 버퍼를 채운다. push 는 FPS 로 솎아 내므로 시간을 앞당겨 준다."""
    for i in range(n):
        EV._last_push[CAM] = 0.0     # 솎아내기 우회
        EV.push(CAM, _frame(i))


# --- 링 버퍼 ----------------------------------------------------------------

def test_링_버퍼가_직전_장면을_담는다():
    _fill_ring(5)
    assert EV.buffered(CAM) == 5
    assert EV.latest_frame(CAM) is not None


def test_링_버퍼는_상한을_넘지_않는다():
    """39개 지점을 무제한으로 담으면 메모리가 무너진다."""
    _fill_ring(EV.RING_MAX_FRAMES + 20)
    assert EV.buffered(CAM) <= EV.RING_MAX_FRAMES


def test_수집을_시작하면_앞구간이_함께_들어간다():
    _fill_ring(6)
    assert EV.start(101, CAM, "경계") is True
    # 같은 이벤트로 두 번 시작하지 않는다.
    assert EV.start(101, CAM, "경계") is False
    job = EV.pop(101)
    assert job is not None and len(job.frames) == 6


def test_클립과_정지영상이_실제로_써진다(tmp_path):
    _fill_ring(6)
    EV.start(102, CAM, "심각")
    job = EV.pop(102)

    img = EV.write_image(102, EV.latest_frame(CAM))
    assert img is not None
    rel, size, digest = img
    assert size > 0 and len(digest) == 64
    assert EV.abs_path(rel).exists()

    clip = EV.write_clip(102, job.frames)
    assert clip is not None, "vp09 코덱으로 클립을 쓰지 못했습니다"
    assert EV.abs_path(clip[0]).exists() and clip[1] > 0
    # ★ 2026-08-20 부터 webm/vp09 다 — 실제로 브라우저에서 재생을 확인하고
    #   바꾼 값이다(예전 mp4v 는 이 환경에서 인코딩은 되지만 재생이 안 됐다).
    assert clip[0].endswith(".webm")


def test_저장_폴더_밖_경로는_거부한다():
    """경로 조작으로 아무 파일이나 내려받게 두면 안 된다."""
    with pytest.raises(ValueError):
        EV.abs_path("../../.env")


# --- 등급 정책 ---------------------------------------------------------------

def test_기본값은_경계와_심각이다():
    assert S.DEFAULTS[S.KEY_EVIDENCE_LEVELS] == "경계,심각"


def test_고르지_않은_등급은_수집하지_않는다():
    db = get_session()
    try:
        S.set_evidence_levels(db, ["심각"])
        db.commit()
        S.load_all(db)
    finally:
        db.close()
    _fill_ring(4)

    ev = Event(id=None, domain="flood", block_id=CAM, place_name="시험",
               level="경계", peak_level="경계", status=E.OPEN)
    ev.id = 900
    evidence_capture.on_detection(ev, is_new=True)
    assert EV.active_count() == 0      # 「경계」는 고르지 않았다


def test_고른_등급은_수집한다():
    db = get_session()
    try:
        S.set_evidence_levels(db, ["경계", "심각"])
        db.commit()
        S.load_all(db)
    finally:
        db.close()
    _fill_ring(4)

    ev = Event(domain="flood", block_id=CAM, place_name="시험",
               level="경계", peak_level="경계", status=E.OPEN)
    ev.id = 901
    evidence_capture.on_detection(ev, is_new=True)
    assert EV.active_count() == 1


def test_모르는_등급은_저장되지_않는다():
    db = get_session()
    try:
        value = S.set_evidence_levels(db, ["경계", "없는등급"])
        db.commit()
        assert "없는등급" not in value
        assert "경계" in value
    finally:
        db.close()


# --- 화면 -------------------------------------------------------------------

def test_화면에_4개_등급이_모두_제시된다(client):
    r = client.get("/admin/evidence")
    assert r.status_code == 200
    for lv in E.LEVELS:
        assert lv in r.text
    assert 'name="levels"' in r.text


def test_이벤트로_안_올라오는_등급은_경고가_붙는다(client):
    """「관심」을 골라도 안 남는다는 사실을 화면이 말해야 한다."""
    r = client.get("/admin/evidence")
    assert r.status_code == 200
    assert "이벤트로 올리지 않는 등급" in r.text
    # 임계값보다 낮은 등급이 실제로 있어야 이 경고가 의미를 갖는다.
    assert [lv for lv in E.LEVELS if not E.is_reportable(lv)]


def test_등급을_저장하면_반영되고_감사로그가_남는다(client):
    r = client.post("/admin/evidence/levels", data={"levels": ["심각"]},
                    follow_redirects=False)
    assert r.status_code == 303
    db = get_session()
    try:
        assert S.evidence_levels(db) == {"심각"}
        acts = db.scalars(select(AuditLog.action)).all()
        assert "evidence.levels" in acts
    finally:
        db.close()


def test_관심을_고르면_안_남는다고_알려_준다(client):
    dead = [lv for lv in E.LEVELS if not E.is_reportable(lv)][0]
    r = client.post("/admin/evidence/levels", data={"levels": [dead]},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "실제로는 남지 않습니다" in r.text


def _seed_evidence(db, *, level="경계", kind=EV.KIND_IMAGE, path="x.jpg"):
    ev = Event(domain="flood", block_id=CAM, place_name="시험",
               level=level, peak_level=level, status=E.OPEN)
    db.add(ev)
    db.flush()
    row = EventEvidence(event_id=ev.id, camera_id=CAM, domain="flood",
                        level=level, kind=kind, path=path, bytes=1234,
                        sha256="a" * 64)
    db.add(row)
    db.commit()
    return ev, row


def test_등급별로_확인할_수_있다(client):
    db = get_session()
    try:
        _seed_evidence(db, level="심각")
        _seed_evidence(db, level="경계")
    finally:
        db.close()

    r = client.get("/admin/evidence?level=심각")
    assert r.status_code == 200
    # 등급 필터가 실제로 걸려야 한다.
    assert r.text.count("EVID-CAM") >= 1


def test_삭제하면_행과_파일이_함께_사라진다(client, tmp_path):
    # 실제 파일을 하나 만들어 둔다 — DB만 지우면 주인 없는 영상이 남는다.
    blob = EV._encode(_frame(1), 80)
    saved = EV.write_image(777, blob)
    assert saved is not None
    rel = saved[0]
    assert EV.abs_path(rel).exists()

    db = get_session()
    try:
        _, row = _seed_evidence(db, path=rel)
        rid = row.id
    finally:
        db.close()

    r = client.post("/admin/evidence/delete", data={"ids": [rid]},
                    follow_redirects=False)
    assert r.status_code == 303

    db = get_session()
    try:
        assert db.get(EventEvidence, rid) is None
        assert "evidence.delete" in db.scalars(select(AuditLog.action)).all()
    finally:
        db.close()
    assert not EV.abs_path(rel).exists(), "파일이 남았습니다"


def test_등급_일괄_삭제(client):
    db = get_session()
    try:
        _seed_evidence(db, level="주의", path="a.jpg")
        _seed_evidence(db, level="주의", path="b.jpg")
        _seed_evidence(db, level="심각", path="c.jpg")
    finally:
        db.close()

    r = client.post("/admin/evidence/delete-level", data={"level": "주의"},
                    follow_redirects=False)
    assert r.status_code == 303

    db = get_session()
    try:
        left = db.scalars(select(EventEvidence.level)).all()
        assert "주의" not in left and "심각" in left
    finally:
        db.close()


def test_내려받기는_감사_로그에_남는다(client):
    blob = EV._encode(_frame(2), 80)
    saved = EV.write_image(778, blob)
    db = get_session()
    try:
        _, row = _seed_evidence(db, path=saved[0])
        rid = row.id
    finally:
        db.close()

    r = client.get(f"/admin/evidence/{rid}/file")
    assert r.status_code == 200
    db = get_session()
    try:
        assert "evidence.download" in db.scalars(select(AuditLog.action)).all()
    finally:
        db.close()


def test_팝업_확인은_내려받기와_다른_감사코드로_남는다(client):
    """★ 확인까지 「반출」로 적으면 대장이 노이즈로 가득 차 정작 밖으로
    나간 건을 못 찾는다 — 그래서 `evidence.view` 로 따로 남긴다."""
    blob = EV._encode(_frame(2), 80)
    saved = EV.write_image(779, blob)
    db = get_session()
    try:
        _, row = _seed_evidence(db, path=saved[0])
        rid = row.id
    finally:
        db.close()

    r = client.get(f"/admin/evidence/{rid}/file?inline=1")
    assert r.status_code == 200
    # ⚠️ `filename=` 을 주면 브라우저가 받아 버린다 — inline 응답에는 없어야 한다.
    assert "attachment" not in r.headers.get("content-disposition", "")
    db = get_session()
    try:
        assert "evidence.view" in db.scalars(select(AuditLog.action)).all()
    finally:
        db.close()


def test_관제요원은_보되_지우지_못한다(anon_client, seeded_users, login):
    db = get_session()
    try:
        _, row = _seed_evidence(db)
        rid = row.id
    finally:
        db.close()

    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.get("/admin/evidence").status_code == 200
        r = anon_client.post("/admin/evidence/delete", data={"ids": [rid]},
                             follow_redirects=False)
        assert r.status_code in (302, 303, 403)
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])

    db = get_session()
    try:
        assert db.get(EventEvidence, rid) is not None
    finally:
        db.close()


def test_권한표():
    assert R.can(R.Role.OPR, R.EVIDENCE, R.Action.VIEW)
    assert not R.can(R.Role.OPR, R.EVIDENCE, R.Action.EDIT)
    assert R.can(R.Role.MGR, R.EVIDENCE, R.Action.EDIT)
    assert R.can(R.Role.SYS, R.EVIDENCE, R.Action.EDIT)
    # 등급 설정은 시스템관리자만.
    assert not R.can(R.Role.MGR, R.SETTINGS_SYS, R.Action.EDIT)


# --- 보존기간 자동 파기 -------------------------------------------------------

def test_개월_계산은_달력으로_한다():
    """「3개월」이라고 해 놓고 90일에 지우면 달마다 기준이 달라져 규정과
    어긋난다."""
    from datetime import datetime, timezone
    now = EV._now()
    got = EV.months_ago(3)
    assert got < now
    # 대략 3개월(88~93일) 안에 들어와야 한다.
    assert 85 <= (now - got).days <= 95
    # 0이면 지금 — 아무것도 지우지 않는다는 뜻.
    assert (now - EV.months_ago(0)).total_seconds() < 5


def test_말일_보정():
    """3월 31일의 1개월 전은 2월 28/29일이어야 한다(2월 31일은 없다)."""
    import datetime as dt
    from unittest.mock import patch
    fixed = dt.datetime(2026, 3, 31, 12, 0, tzinfo=dt.timezone.utc)
    with patch.object(EV, "_now", lambda: fixed):
        got = EV.months_ago(1)
    assert got.year == 2026 and got.month == 2
    assert got.day in (28, 29)


def test_기본은_자동_파기_안_함():
    """사람이 정하지 않았는데 시스템이 자료를 지우기 시작하면 안 된다."""
    assert S.DEFAULTS[S.KEY_EVIDENCE_RETENTION_MONTHS] == "0"
    assert S.evidence_retention_months() == 0


def test_목록에_없는_값은_0으로_눕는다():
    db = get_session()
    try:
        assert S.set_evidence_retention_months(db, 7) == 0     # 목록에 없음
        assert S.set_evidence_retention_months(db, "abc") == 0
        assert S.set_evidence_retention_months(db, 3) == 3
    finally:
        db.rollback()
        db.close()


def test_보존기간을_저장하면_감사로그가_남는다(client):
    r = client.post("/admin/evidence/retention", data={"months": "3"},
                    follow_redirects=False)
    assert r.status_code == 303
    db = get_session()
    try:
        assert S.evidence_retention_months(db) == 3
        assert "evidence.retention" in db.scalars(select(AuditLog.action)).all()
    finally:
        db.close()


def test_파기_대상_건수를_미리_알려_준다(client):
    """「3개월」을 골랐는데 그 자리에서 수백 건이 사라지면 몰랐다는 말이 나온다."""
    import datetime as dt
    db = get_session()
    try:
        _, row = _seed_evidence(db, path="old.jpg")
        row.captured_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        db.commit()
    finally:
        db.close()

    r = client.post("/admin/evidence/retention", data={"months": "3"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "자동 파기됩니다" in r.text


def test_자동_파기가_오래된_자료를_지운다():
    """워커가 실제로 지우는지 — 파일과 행 모두."""
    import datetime as dt
    from tot_dashboard.service.evidence_capture import EvidenceWorker

    blob = EV._encode(_frame(3), 80)
    saved = EV.write_image(555, blob)
    assert saved is not None
    rel = saved[0]

    db = get_session()
    try:
        S.set_evidence_retention_months(db, 1)
        db.commit()
        S.load_all(db)
        _, row = _seed_evidence(db, path=rel)
        row.captured_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        db.commit()
        rid = row.id
    finally:
        db.close()

    worker = EvidenceWorker()
    worker._purge_expired()          # 스레드를 띄우지 않고 한 번만 돌린다

    db = get_session()
    try:
        assert db.get(EventEvidence, rid) is None, "행이 남았습니다"
        acts = db.scalars(select(AuditLog.action)).all()
        assert "evidence.purge" in acts
    finally:
        db.close()
    assert not EV.abs_path(rel).exists(), "파일이 남았습니다"


def test_자동_파기가_꺼져_있으면_아무것도_안_지운다():
    import datetime as dt
    from tot_dashboard.service.evidence_capture import EvidenceWorker

    db = get_session()
    try:
        S.set_evidence_retention_months(db, 0)
        db.commit()
        S.load_all(db)
        _, row = _seed_evidence(db, path="keep.jpg")
        row.captured_at = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
        db.commit()
        rid = row.id
    finally:
        db.close()

    EvidenceWorker()._purge_expired()

    db = get_session()
    try:
        assert db.get(EventEvidence, rid) is not None
    finally:
        db.close()


def test_화면에_보존기간_설정이_있다(client):
    r = client.get("/admin/evidence")
    assert r.status_code == 200
    assert "보존기간" in r.text
    assert 'name="months"' in r.text
    # 꺼져 있으면 그 사실을 경고로 알려야 한다.
    assert "자동 파기가 꺼져 있습니다" in r.text
