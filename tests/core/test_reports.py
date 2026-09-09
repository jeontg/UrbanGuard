"""현장 제보 접수·검토 (S-43).

두 가지가 핵심이다.
  1. **원본 사진이 남지 않는가** — 개인정보 보관을 최소화한다
  2. **경로 조작으로 다른 파일을 읽을 수 없는가** — 사진 URL이 파일명을 받는다
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import image_mask
from tot_dashboard.core import reports as RP
from tot_dashboard.core import roles as R


@pytest.fixture
def photo_dir(tmp_path, monkeypatch):
    d = tmp_path / "photos"
    monkeypatch.setenv("URBANGUARD_REPORT_DIR", str(d))
    yield d
    monkeypatch.delenv("URBANGUARD_REPORT_DIR", raising=False)


@pytest.fixture
def db(db_schema, photo_dir):
    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import CitizenReport, Event, EventAction, User
    s = get_session()
    s.query(CitizenReport).delete()
    s.query(EventAction).delete()
    s.query(Event).delete()
    s.query(User).filter(User.login_id.like("rp_%")).delete(synchronize_session=False)
    s.commit()
    create_user(s, login_id="rp_opr", name="rp_opr", dept="테스트",
                role=R.Role.OPR.value, password="ReportTest!2026",
                domains=[d.value for d in R.Domain], must_change=False)
    s.commit()
    yield s
    s.query(CitizenReport).delete()
    s.query(EventAction).delete()
    s.query(Event).delete()
    s.query(User).filter(User.login_id.like("rp_%")).delete(synchronize_session=False)
    s.commit()
    s.close()


@pytest.fixture
def user(db):
    from tot_dashboard.core.models import User
    return db.query(User).filter(User.login_id == "rp_opr").one()


def _jpeg(tmp_path, name="shot.jpg"):
    """작은 실제 JPEG 을 만든다 — 마스킹 경로가 이미지를 실제로 디코드한다."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    p = tmp_path / name
    img = np.full((80, 120, 3), 200, dtype=np.uint8)
    ok, enc = cv2.imencode(".jpg", img)
    assert ok
    enc.tofile(str(p))
    return p


# --- 업로드 검증 -------------------------------------------------------------
@pytest.mark.parametrize("name", ["a.txt", "a.exe", "a.svg", "noext"])
def test_disallowed_extensions_are_rejected(name):
    assert RP.validate_upload(name, 1000)


def test_oversized_file_is_rejected():
    errs = RP.validate_upload("a.jpg", RP.MAX_BYTES + 1)
    assert any("너무 큽니다" in e for e in errs)


def test_empty_file_is_rejected():
    assert RP.validate_upload("a.jpg", 0)


def test_normal_photo_passes():
    assert RP.validate_upload("a.jpg", 1024) == []


# --- 접수 -------------------------------------------------------------------
def test_original_is_not_kept(db, user, tmp_path, photo_dir):
    """원본을 남기면 마스킹의 의미가 없다."""
    src = _jpeg(tmp_path)
    row = RP.create(db, raw_path=src, filename="shot.jpg", user=user,
                    place_name="중앙대로", description="포트홀")
    db.commit()
    assert not src.exists()
    assert row.photo_path
    assert (photo_dir / row.photo_path).exists()


def test_stored_filename_is_not_the_uploaded_one(db, user, tmp_path):
    """올린 이름을 그대로 쓰면 경로 조작·한글 경로 문제가 생긴다."""
    src = _jpeg(tmp_path, "한글 이름.jpg")
    row = RP.create(db, raw_path=src, filename="한글 이름.jpg", user=user)
    db.commit()
    assert "한글" not in row.photo_path
    assert row.photo_path.endswith(".jpg")


def test_mask_status_is_recorded(db, user, tmp_path):
    src = _jpeg(tmp_path)
    row = RP.create(db, raw_path=src, filename="shot.jpg", user=user)
    db.commit()
    assert row.mask_status in image_mask.STATUS_LABELS


def test_report_starts_as_received(db, user, tmp_path):
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    db.commit()
    assert row.status == RP.RECEIVED
    assert RP.counts(db)[RP.RECEIVED] == 1


# --- 검토 -------------------------------------------------------------------
def test_review_marks_training_usability(db, user, tmp_path):
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    RP.review(db, row, user, usable=True, memo="선명함")
    db.commit()
    assert row.status == RP.REVIEWED and row.usable_for_training is True
    assert RP.training_ready(db) == 1


def test_unusable_photo_is_not_counted_for_training(db, user, tmp_path):
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    RP.review(db, row, user, usable=False, memo="초점 나감")
    db.commit()
    assert RP.training_ready(db) == 0


def test_convert_links_an_event(db, user, tmp_path):
    from tot_dashboard.core import events as E
    ev = E.record_detection(db, domain="road", block_id="R1",
                            place_name="제보 지점", level="주의")
    db.flush()
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    RP.convert(db, row, user, ev.id, "보수 요청")
    db.commit()
    assert row.status == RP.CONVERTED and row.event_id == ev.id


def test_reject_records_reason(db, user, tmp_path):
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    RP.reject(db, row, user, "노면이 안 보임")
    db.commit()
    assert row.status == RP.REJECTED and "노면" in row.disposition


# --- 사진 파기 ---------------------------------------------------------------
def test_photo_can_be_destroyed(db, user, tmp_path, photo_dir):
    """개인정보 문제가 발견되면 즉시 지울 수 있어야 한다."""
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    db.commit()
    p = photo_dir / row.photo_path
    assert p.exists()
    assert RP.delete_photo(row) is True
    assert not p.exists() and row.photo_path == ""


def test_destroyed_photo_leaves_training_count_correct(db, user, tmp_path):
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    RP.review(db, row, user, usable=True)
    RP.delete_photo(row)
    db.commit()
    assert RP.training_ready(db) == 0


# --- 경로 조작 방어 -----------------------------------------------------------
@pytest.mark.parametrize("name", [
    "../../.env", "..\\..\\.env", "/etc/passwd", "a.jpg/../../x",
    "a;rm.jpg", "", "a.exe", "a.jpg\x00.txt",
])
def test_path_traversal_is_blocked(photo_dir, name):
    assert RP.safe_photo_path(name) is None


def test_valid_photo_name_resolves(db, user, tmp_path, photo_dir):
    row = RP.create(db, raw_path=_jpeg(tmp_path), filename="a.jpg", user=user)
    db.commit()
    assert RP.safe_photo_path(row.photo_path) is not None


def test_nonexistent_but_wellformed_name_returns_none(photo_dir):
    assert RP.safe_photo_path("20260101_deadbeef.jpg") is None


# --- 목록 -------------------------------------------------------------------
def test_open_filter_covers_received_and_reviewed(db, user, tmp_path):
    a = RP.create(db, raw_path=_jpeg(tmp_path, "a.jpg"), filename="a.jpg", user=user)
    b = RP.create(db, raw_path=_jpeg(tmp_path, "b.jpg"), filename="b.jpg", user=user)
    RP.review(db, b, user, usable=False)
    c = RP.create(db, raw_path=_jpeg(tmp_path, "c.jpg"), filename="c.jpg", user=user)
    RP.reject(db, c, user, "x")
    db.commit()
    ids = {r.id for r in RP.list_reports(db, status="open")}
    assert ids == {a.id, b.id}
