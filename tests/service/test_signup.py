"""가입 신청 — 로그인 화면 셀프서비스 신청 → 관리자 승인 (2026-09-01 신설).

지켜야 할 것.

* **비인증으로 열린다** — `/signup`은 `core/guard.py::PUBLIC`에 있다.
* **중복 아이디(기존 계정·대기 중인 신청)는 접수 단계에서 막는다.**
* **같은 IP의 짧은 시간 반복 제출은 막는다**(2026-09-01 사용자 결정 —
  최소한의 자체 제한, 외부 CAPTCHA는 이번 범위에 없다).
* **신청자가 직접 입력한 비밀번호가 승인 후 그대로 로그인에 쓰인다** —
  관리자는 비밀번호를 모른다(가장 중요한 end-to-end 확인).
* **승인·거절은 시스템관리자만** — 다른 역할은 403.
* **이중 처리를 막는다** — 이미 처리된 신청은 다시 승인/거절할 수 없다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.core import signup_requests as SR
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import SignupRequest, User
from tot_dashboard.service.main import app

VALID_PW = "Signup!Test9"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def _clean_signup_requests(db_schema):
    """매 시험 전후로 가입 신청 표를 비운다 — IP 남용 제한이 이전 시험의
    신청 건수를 이어받아 오탐하지 않게 한다."""
    def _purge():
        db = get_session()
        try:
            db.query(SignupRequest).delete()
            db.commit()
        finally:
            db.close()
    _purge()
    yield
    _purge()


def _payload(login_id: str, **overrides) -> dict:
    data = {
        "login_id": login_id, "name": "홍길동", "dept": "정보통신과",
        "password": VALID_PW, "password_confirm": VALID_PW,
        "role": "OPR", "reason": "테스트 신청",
    }
    data.update(overrides)
    return data


def test_비인증으로_가입신청_화면이_열린다(anon_client):
    res = anon_client.get("/signup")
    assert res.status_code == 200
    assert "가입 신청" in res.text


def test_신청이_접수되면_대기_상태로_저장된다(anon_client, db_schema):
    res = anon_client.post("/signup", data=_payload("signup_ok1"))
    assert res.status_code == 200
    assert "접수됐습니다" in res.text

    db = get_session()
    try:
        req = db.query(SignupRequest).filter_by(login_id="signup_ok1").one()
        assert req.status == SR.PENDING
        assert req.requested_role == "OPR"
        # 평문 비밀번호는 어디에도 남지 않는다 — 해시만 저장된다.
        assert req.pw_hash != VALID_PW
        assert req.pw_hash.startswith("$")  # bcrypt 해시 형식
    finally:
        db.close()


def test_비밀번호_확인이_다르면_거부된다(anon_client):
    res = anon_client.post("/signup", data=_payload(
        "signup_pwmismatch", password_confirm="Different!9"))
    assert res.status_code == 400
    assert "다릅니다" in res.text


def test_비밀번호_정책_위반은_거부된다(anon_client):
    res = anon_client.post("/signup", data=_payload(
        "signup_weakpw", password="short", password_confirm="short"))
    assert res.status_code == 400


def test_아이디_형식이_틀리면_거부된다(anon_client):
    res = anon_client.post("/signup", data=_payload("아이디한글"))
    assert res.status_code == 400
    assert "영문" in res.text


def test_이미_존재하는_계정_아이디는_거부된다(anon_client, seeded_users):
    admin_id, _ = seeded_users["admin"]
    res = anon_client.post("/signup", data=_payload(admin_id))
    assert res.status_code == 400
    assert "이미 사용 중" in res.text


def test_이미_대기중인_신청과_같은_아이디는_거부된다(anon_client):
    anon_client.post("/signup", data=_payload("signup_dup1"))
    res = anon_client.post("/signup", data=_payload("signup_dup1"))
    assert res.status_code == 400
    assert "이미 접수된 신청" in res.text


def test_같은_IP에서_짧은_시간_반복_제출은_막힌다(anon_client):
    for i in range(3):
        anon_client.post("/signup", data=_payload(f"signup_throttle{i}"))
    res = anon_client.post("/signup", data=_payload("signup_throttle_over"))
    assert res.status_code == 429
    assert "잠시 후" in res.text


def test_관제요원은_승인거절_라우트에_403(anon_client, seeded_users, login):
    anon_client.post("/signup", data=_payload("signup_opr403"))
    db = get_session()
    try:
        req = db.query(SignupRequest).filter_by(login_id="signup_opr403").one()
        req_id = req.id
    finally:
        db.close()

    anon_client.cookies.clear()
    login(anon_client, *seeded_users["opr"])
    try:
        res = anon_client.post(f"/admin/users/signup/{req_id}/approve",
                               data={"role": "OPR"})
        assert res.status_code == 403
        res = anon_client.post(f"/admin/users/signup/{req_id}/reject", data={})
        assert res.status_code == 403
    finally:
        anon_client.cookies.clear()


def test_관리자_승인시_신청자가_직접_입력한_비밀번호로_로그인된다(
        client, anon_client, db_schema):
    """★ 가장 중요한 확인 — 관리자는 비밀번호를 모르고, 신청자가 신청서에
    직접 입력했던 바로 그 비밀번호가 승인 후 그대로 로그인에 쓰인다."""
    res = anon_client.post("/signup", data=_payload(
        "signup_login_e2e", role="MGR"))
    assert res.status_code == 200
    db = get_session()
    try:
        req = db.query(SignupRequest).filter_by(
            login_id="signup_login_e2e").one()
        req_id = req.id
    finally:
        db.close()

    res = client.post(f"/admin/users/signup/{req_id}/approve",
                      data={"role": "MGR", "domains": ["flood"]})
    assert res.status_code == 200
    assert "승인" in res.text

    db = get_session()
    try:
        req = db.query(SignupRequest).filter_by(id=req_id).one()
        assert req.status == SR.APPROVED
        assert req.reviewed_by is not None
        user = db.query(User).filter_by(login_id="signup_login_e2e").one()
        assert user.role == "MGR"
        assert "flood" in user.domain_set
        assert user.must_change_password is False
        assert req.created_user_id == user.id
    finally:
        db.close()

    # 신청자 본인이 신청서에 적었던 바로 그 비밀번호로 실제 로그인.
    fresh = TestClient(app)
    res = fresh.post("/login", data={
        "login_id": "signup_login_e2e", "password": VALID_PW, "next": "/",
    }, follow_redirects=False)
    assert res.status_code == 303, f"로그인 실패: {res.text}"


def test_거절하면_계정이_생기지_않는다(client, anon_client, db_schema):
    anon_client.post("/signup", data=_payload("signup_rejected1"))
    db = get_session()
    try:
        req = db.query(SignupRequest).filter_by(
            login_id="signup_rejected1").one()
        req_id = req.id
    finally:
        db.close()

    res = client.post(f"/admin/users/signup/{req_id}/reject",
                      data={"reason": "정원 초과"})
    assert res.status_code == 200
    assert "거절" in res.text

    db = get_session()
    try:
        req = db.query(SignupRequest).filter_by(id=req_id).one()
        assert req.status == SR.REJECTED
        assert req.reject_reason == "정원 초과"
        assert db.query(User).filter_by(
            login_id="signup_rejected1").one_or_none() is None
    finally:
        db.close()


def test_이미_처리된_신청은_다시_승인거절할_수_없다(client, anon_client, db_schema):
    anon_client.post("/signup", data=_payload("signup_idempotent1"))
    db = get_session()
    try:
        req = db.query(SignupRequest).filter_by(
            login_id="signup_idempotent1").one()
        req_id = req.id
    finally:
        db.close()

    res = client.post(f"/admin/users/signup/{req_id}/approve",
                      data={"role": "OPR"})
    assert res.status_code == 200

    # 두 번째 승인 시도 — 이미 계정이 생겼는데 또 만들면 안 된다.
    res = client.post(f"/admin/users/signup/{req_id}/approve",
                      data={"role": "OPR"})
    assert res.status_code == 400
    assert "이미 처리된 신청" in res.text

    res = client.post(f"/admin/users/signup/{req_id}/reject", data={})
    assert res.status_code == 400

    db = get_session()
    try:
        count = db.query(User).filter_by(
            login_id="signup_idempotent1").count()
        assert count == 1, "이중 승인으로 계정이 두 번 만들어졌다"
    finally:
        db.close()


def test_로그인_화면이_대기중인_신청을_안내한다(anon_client):
    anon_client.post("/signup", data=_payload("signup_pending_login"))
    res = anon_client.post("/login", data={
        "login_id": "signup_pending_login", "password": "아무거나아무거나",
        "next": "/",
    })
    assert res.status_code == 401
    assert "승인 대기" in res.text
