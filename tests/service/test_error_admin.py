"""오류 관리 화면 (S-92 발생 이력 · S-93 오류 코드 사전).

지켜야 할 것.

* **시스템관리자 전용** — 관제요원에게는 화면도 메뉴도 보이지 않는다. 오류
  메시지에는 내부 경로·질의가 그대로 실려 오기 때문이다
* **원인·해결방법이 이력 화면에 함께 나온다** — 다른 화면으로 옮겨 다니게 하면
  아무도 읽지 않는다
* **삭제 흔적은 감사 로그에 남는다** — 오류 이력 자체는 실제로 지워지므로,
  무엇을 몇 건 지웠는지는 감사 로그에만 남는다
* **검색 조건은 조치·삭제 후에도 유지된다** — 방금 무엇을 보고 있었는지 잃으면
  같은 조건을 매번 다시 넣게 된다
* **처리되지 않은 예외는 오류 이력에 남는다** — 이게 안 되면 화면은 500을
  띄우고 기록은 아무 데도 없다
"""
from __future__ import annotations

import pytest

# 임포트 순서에 좌우되는 환경변수(블록 목록·알림 발송기)는 tests/conftest.py 가
# 잡는다. 여기서 잡으면 이 모듈이 먼저 임포트될 때만 맞고 아닐 때는 어긋난다.
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete as sa_delete, select  # noqa: E402

from tot_dashboard.core import errors as E  # noqa: E402
from tot_dashboard.core.db import get_session  # noqa: E402
from tot_dashboard.core.models import AuditLog, ErrorCode, ErrorLog  # noqa: E402
from tot_dashboard.service.main import app  # noqa: E402


@pytest.fixture(scope="module")
def anon_client():
    """수명주기(lifespan)를 **켜지 않고** 클라이언트를 만든다.

    ``with TestClient(app)`` 로 열면 기동 훅이 돌면서 ``main.py`` 의 모듈 수준
    ``runner`` 스레드를 start 한다. 그 스레드는 프로세스당 한 번만 시작할 수
    있어, 다른 시험 모듈(``test_main.py``)이 뒤이어 열 때 전부 깨진다.
    이 화면들은 기동 훅이 필요 없으므로 열지 않는다.
    """
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    db = get_session()
    try:
        E.seed_builtin(db)
    finally:
        db.close()
    yield
    _purge()


def _purge():
    s = get_session()
    try:
        s.execute(sa_delete(ErrorLog))
        s.execute(sa_delete(ErrorCode))
        s.commit()
    finally:
        s.close()


# --- 권한 -------------------------------------------------------------------
def test_관제요원은_오류_화면에_들어갈_수_없다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.get("/admin/errors").status_code == 403
        assert anon_client.get("/admin/error-codes").status_code == 403
    finally:
        anon_client.cookies.clear()


def test_관제요원_메뉴에는_오류_관리가_없다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        body = anon_client.get("/").text
        assert "/admin/errors" not in body
    finally:
        anon_client.cookies.clear()


def test_시스템관리자_메뉴에는_오류_관리가_있다(client):
    body = client.get("/").text
    assert "/admin/errors" in body
    assert "/admin/error-codes" in body


# --- 발생 이력 ---------------------------------------------------------------
def test_이력_화면에_원인과_해결방법이_함께_나온다(client):
    E.record(code="UG-CCTV-002", message="getaddrinfo failed", source="worker")
    body = client.get("/admin/errors").text
    assert "UG-CCTV-002" in body
    # 사전의 해결방법 문구가 목록 안에 실려야 한다.
    assert "nslookup" in body


def test_사전에_없는_코드는_그렇다고_알려_준다(client):
    E.record(code="UG-NEW-999", message="처음 보는 오류")
    body = client.get("/admin/errors").text
    assert "UG-NEW-999" in body
    assert "사전에 없는 코드" in body


def test_검색_조건이_먹는다(client):
    # 사전에 실린 오류명과 겹치지 않는 문구를 쓴다 — 겹치면 걸러졌는지
    # 아닌지를 화면만 보고 판정할 수 없다.
    E.record(code="UG-CCTV-001", message="표식가 발생", path="/a")
    E.record(code="UG-DB-001", message="표식나 발생", path="/b")

    # 코드 자체는 상단 요약(최근 7일 전체)에도 나오므로, 목록에 실제로 걸러진
    # 것은 **메시지**로 확인한다.
    only_cctv = client.get("/admin/errors", params={"code": "UG-CCTV-001"}).text
    assert "표식가" in only_cctv
    assert "표식나" not in only_cctv

    by_word = client.get("/admin/errors", params={"q": "표식나"}).text
    assert "표식나" in by_word
    assert "표식가" not in by_word


def test_처리완료로_표시하면_상태가_바뀐다(client):
    rid = E.record(code="UG-CCTV-001", message="스트림 열기 실패")
    res = client.post("/admin/errors/resolve",
                      data={"log_id": rid, "note": "회선 교체함"})
    assert res.status_code == 200
    db = get_session()
    try:
        row = db.get(ErrorLog, rid)
        assert row.resolved is True
        assert row.resolve_note == "회선 교체함"
        assert row.resolved_by  # 누가 했는지 남아야 한다
    finally:
        db.close()


def test_삭제하면_사라지고_감사_로그에_남는다(client):
    rid = E.record(code="UG-CCTV-001", message="지울 오류")
    res = client.post("/admin/errors/delete", data={"log_ids": [rid]})
    assert res.status_code == 200

    db = get_session()
    try:
        assert db.get(ErrorLog, rid) is None, "오류 이력이 실제로 지워져야 한다"
        rows = db.scalars(
            select(AuditLog).where(AuditLog.action == "error.delete")).all()
        assert rows, "삭제 사실이 감사 로그에 남지 않았다"
        # 무엇을 지웠는지까지 남아야 「장애 기록을 지웠다」는 물음에 답할 수 있다.
        assert "지울 오류" in str(rows[-1].before)
    finally:
        db.close()


def test_삭제_후에도_검색_조건이_유지된다(client):
    rid = E.record(code="UG-CCTV-001", message="지울 오류", path="/a")
    E.record(code="UG-DB-001", message="남을 오류", path="/b")
    res = client.post("/admin/errors/delete?code=UG-CCTV-001",
                      data={"log_ids": [rid]})
    assert res.status_code == 200
    # 조건이 풀렸다면 다른 코드의 오류까지 목록에 나타난다.
    assert "남을 오류" not in res.text


def test_선택한_항목이_없으면_아무것도_지우지_않는다(client):
    rid = E.record(code="UG-CCTV-001", message="남아야 할 오류")
    res = client.post("/admin/errors/delete", data={})
    assert res.status_code == 200
    assert "선택한 항목이 없습니다" in res.text
    db = get_session()
    try:
        assert db.get(ErrorLog, rid) is not None
    finally:
        db.close()


def test_일괄_정리는_기본으로_처리완료_건만_지운다(client):
    from datetime import timedelta
    keep = E.record(code="UG-CCTV-001", message="미처리", path="/a")
    gone = E.record(code="UG-DB-001", message="처리함", path="/b")
    db = get_session()
    try:
        E.resolve(db, gone, by="admin")
        for rid in (keep, gone):
            row = db.get(ErrorLog, rid)
            row.last_seen_at = row.last_seen_at - timedelta(days=99)
        db.commit()
    finally:
        db.close()

    res = client.post("/admin/errors/cleanup",
                      data={"before_days": 30, "resolved_only": "1"})
    assert res.status_code == 200
    db = get_session()
    try:
        assert db.get(ErrorLog, keep) is not None, "미처리 오류가 함께 지워졌다"
        assert db.get(ErrorLog, gone) is None
    finally:
        db.close()


# --- 오류 코드 사전 ----------------------------------------------------------
def test_사전_화면에_기본_코드가_나온다(client):
    body = client.get("/admin/error-codes").text
    assert "UG-CCTV-002" in body
    assert "UG-AUTH-005" in body


def test_코드를_등록한다(client):
    res = client.post("/admin/error-codes/save", data={
        "code": "UG-TEST-100", "category": "SYS", "title": "시험용 오류",
        "severity": "warn", "cause": "원인 설명", "resolution": "조치 방법",
        "is_active": "1"})
    assert res.status_code == 200
    assert "UG-TEST-100" in res.text
    db = get_session()
    try:
        row = E.get_code(db, "UG-TEST-100")
        assert row is not None and row.title == "시험용 오류"
        assert row.builtin is False
    finally:
        db.close()


def test_잘못된_코드는_입력값을_돌려주며_거부한다(client):
    res = client.post("/admin/error-codes/save", data={
        "code": "잘못된 코드", "category": "SYS", "title": "제목",
        "severity": "error", "cause": "원인 그대로", "resolution": "",
        "is_active": "1"})
    assert res.status_code == 400
    # 다시 채워 넣지 않아도 되게 입력값이 폼에 남아야 한다.
    assert "원인 그대로" in res.text


def test_기본_제공_코드는_삭제할_수_없다(client):
    res = client.post("/admin/error-codes/delete", data={"code": "UG-CCTV-001"})
    assert res.status_code == 400
    assert "삭제할 수 없습니다" in res.text
    db = get_session()
    try:
        assert E.get_code(db, "UG-CCTV-001") is not None
    finally:
        db.close()


def test_직접_등록한_코드는_삭제할_수_있다(client):
    client.post("/admin/error-codes/save", data={
        "code": "UG-TEST-101", "category": "SYS", "title": "지울 코드",
        "severity": "error", "cause": "", "resolution": "", "is_active": "1"})
    res = client.post("/admin/error-codes/delete", data={"code": "UG-TEST-101"})
    assert res.status_code == 200
    db = get_session()
    try:
        assert E.get_code(db, "UG-TEST-101") is None
    finally:
        db.close()


def test_사전_화면이_코드별_발생_건수를_보여_준다(client):
    E.record(code="UG-CCTV-001", message="a", path="/1")
    E.record(code="UG-CCTV-001", message="b", path="/2")
    body = client.get("/admin/error-codes").text
    assert "2회" in body


# --- 자동 수집 ---------------------------------------------------------------
def test_처리되지_않은_예외가_오류_이력에_남는다(client):
    """이게 안 되면 화면은 500을 띄우고 기록은 아무 데도 없다."""
    @app.get("/api/_boom_for_test")
    def _boom():
        raise RuntimeError("일부러 낸 오류")

    # 새 경로는 권한 규칙표에 없으므로 가드가 먼저 막는다. 그 차단 자체가
    # UG-AUTH-005 로 기록되는지도 함께 확인한다.
    res = client.get("/api/_boom_for_test")
    assert res.status_code == 403

    db = get_session()
    try:
        rows = E.search(db, code="UG-AUTH-005")
        assert rows, "권한 규칙 미등록 차단이 오류 이력에 남지 않았다"
        assert "_boom_for_test" in rows[0].message
    finally:
        db.close()


def test_백그라운드_ERROR_로그가_오류_이력에_올라온다(client):
    """워처·파이프라인은 요청 밖에서 돈다. 이 경로가 없으면 아무도 못 본다."""
    import logging

    from tot_dashboard.core import error_hook
    error_hook.install()
    logging.getLogger("urbanguard.road").error("상시 순회 중 스트림 열기 실패")

    db = get_session()
    try:
        rows = E.search(db, source="worker")
        assert rows, "백그라운드 오류가 수집되지 않았다"
        assert "스트림 열기 실패" in rows[0].message
    finally:
        db.close()


def test_오류_기록_모듈_자신의_로그는_수집하지_않는다(client):
    """기록 실패를 기록하려다 무한히 도는 것을 막는다."""
    import logging

    from tot_dashboard.core import error_hook
    error_hook.install()
    logging.getLogger("urbanguard.errors").error("오류 기록 실패")

    db = get_session()
    try:
        assert not E.search(db, q="오류 기록 실패")
    finally:
        db.close()
