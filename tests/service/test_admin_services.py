"""서비스 관리(관리자 전용, 2026-09-01 사용자 요청 신설) — 5개 서비스의
현재 가동 정보·상태를 한 화면에서 본다.

지켜야 할 것.

* **시스템관리자만 본다** — 다른 역할은 메뉴에도 화면에도 접근 못 한다.
* **응답 없는 서비스가 있어도 화면 자체는 뜬다** — 정직한 실패
  (``routes_services.py`` 머리말의 짧은 시간제한·병렬 확인 설계).
* **pid·uptime_sec·supervised는 전용 칸으로 빠지고, "세부 정보"에는
  중복되지 않는다** — 안 걸러지면 화면에 같은 값이 두 번 보인다.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tot_dashboard.core import settings as S
from tot_dashboard.service import routes_services as RS
from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def test_비인증은_로그인으로_보낸다(anon_client):
    res = anon_client.get("/admin/services", follow_redirects=False)
    assert res.status_code == 303
    assert "/login" in res.headers["location"]


def test_관제요원은_못_본다(anon_client, seeded_users, login):
    anon_client.cookies.clear()
    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.get("/admin/services").status_code == 403
    finally:
        anon_client.cookies.clear()


def test_시스템관리자는_5개_서비스를_전부_본다(client, db_schema, monkeypatch):
    """★ 개발 PC에는 실제 4개 서비스가 이미 떠 있을 수도 있어(이 세션이
    그랬다) 원격 확인 결과를 흉내내 결정적으로 시험한다 — 아니면 그 PC의
    현재 상태에 따라 결과가 흔들리는 시험이 된다."""
    monkeypatch.setattr(RS, "_check_remote", lambda port: {
        "ok": False, "latency_ms": 5.0, "detail": {"error": "무시(시험)"}})
    res = client.get("/admin/services")
    assert res.status_code == 200
    for label in ("플랫폼-쉘", "인파관리", "노면관리", "침수", "교통위험"):
        assert label in res.text
    assert "1 / 5" in res.text  # 자기 자신(플랫폼-쉘)만 정상


def test_메뉴에_시스템관리자에게만_보인다(client, anon_client, seeded_users, login):
    from tot_dashboard.core.auth import menu_for
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import User

    db = get_session()
    try:
        sys_user = db.query(User).filter_by(
            login_id=seeded_users["admin"][0]).one()
        opr_user = db.query(User).filter_by(
            login_id=seeded_users["opr"][0]).one()
    finally:
        db.close()

    sys_hrefs = [i["href"] for g in menu_for(sys_user) for i in g["items"]]
    opr_hrefs = [i["href"] for g in menu_for(opr_user) for i in g["items"]]
    assert "/admin/services" in sys_hrefs
    assert "/admin/services" not in opr_hrefs


def test_로컬_확인은_네트워크를_타지_않고_즉시_정상이다():
    r = RS._check_local()
    assert r["ok"] is True
    assert r["latency_ms"] == 0.0
    assert "pid" in r["detail"]
    assert "uptime_sec" in r["detail"]


def test_원격_확인_실패시_예외_대신_ok_false를_돌려준다(monkeypatch):
    """★ 죽은 서비스 하나 때문에 이 화면 자체가 500이 나면 안 된다.
    ``_check_remote``는 매번 새 클라이언트를 만드는 ``httpx.get()``이
    아니라 재사용 클라이언트(``_http_client()``)를 쓰므로 그쪽을
    흉내낸다(2026-09-01 — SSL 컨텍스트 재생성 성능 문제 발견 후 변경)."""
    def _boom(*a, **kw):
        raise httpx.ConnectError("boom")
    monkeypatch.setattr(RS, "_http_client",
                        lambda: type("C", (), {"get": _boom})())
    r = RS._check_remote(59999)
    assert r["ok"] is False
    assert "error" in r["detail"]


def test_원격_확인은_시간제한을_지킨다(monkeypatch):
    """★ 응답 없는 서비스 하나가 화면 전체를 오래 붙잡으면 안 된다 —
    get 호출에 반드시 timeout 인자를 넘기는지 확인한다."""
    called = {}

    def _fake_get(url, timeout=None):
        called["timeout"] = timeout
        raise httpx.TimeoutException("timeout")
    monkeypatch.setattr(RS, "_http_client",
                        lambda: type("C", (), {"get": staticmethod(_fake_get)})())
    RS._check_remote(59999)
    assert called["timeout"] == RS.HEALTH_TIMEOUT_SEC


def test_원격_확인은_클라이언트를_재사용한다(monkeypatch):
    """★ 실기로 확인한 성능 문제(2026-09-01) 재발 방지 — 매 호출마다
    새 `httpx.Client`(SSL 컨텍스트 재생성 포함)를 만들면 안 된다."""
    monkeypatch.setattr(RS, "_HTTP_CLIENT", None)
    a = RS._http_client()
    b = RS._http_client()
    assert a is b


def test_세부정보에서_전용_칸_값은_빠진다(monkeypatch):
    """pid·uptime_sec·supervised는 화면의 전용 칸(PID·가동시간·감시)에
    이미 나오므로, "세부 정보"에 또 나오면 같은 값이 두 번 보인다.
    실제 네트워크 호출 없이(다른 4개 서비스가 시험 환경엔 없다) 결정적으로
    확인하기 위해 원격 확인 결과를 흉내낸다."""
    monkeypatch.setattr(RS, "_check_remote", lambda port: {
        "ok": True, "latency_ms": 1.0,
        "detail": {"pid": 999, "uptime_sec": 1.0, "supervised": True,
                   "block_count": 3}})
    services = RS._collect()
    for s in services:
        assert "pid" not in s["extra"]
        assert "uptime_sec" not in s["extra"]
        assert "supervised" not in s["extra"]
    remote = next(s for s in services if s["key"] != "main")
    assert remote["extra"] == {"block_count": 3}


# ── 정지/시작 (2026-09-01 신설) ──────────────────────────────────────────
#
# ⚠️ `_launch_script`를 반드시 monkeypatch한다 — 이 개발 PC엔 실제 4개
# 서비스가 떠 있고, 시험이 진짜 stop-*.ps1/ensure-*.ps1을 돌리면 그
# 서비스들을 실제로 죽이거나 새로 띄운다. 절대 안 된다.

@pytest.fixture(autouse=True)
def _no_real_process_control(monkeypatch, tmp_path):
    """이 파일의 모든 시험에 적용 — 실제 프로세스 제어를 원천 차단한다.

    ⚠️ `RS._IN_PROGRESS`(2026-09-02 신설, 중복 정지/시작 방지 잠금)를
    앞뒤로 비운다 — 아니면 한 시험에서 잠금이 안 풀린 채 남아 이후
    시험이 예상 밖 409를 받는다. 기본 mock은 `None`을 돌려주므로
    `_try_start_operation`이 (기다릴 실제 프로세스가 없어) 곧바로
    잠금을 풀어, 이 mock을 그대로 쓰는 기존 시험은 동작 변화가 없다.

    ⚠️ `S._SERVICE_STATE_FILE`(2026-09-02 신설, "관리자가 정지시킴"
    미러 파일)도 임시 경로로 바꿔치기한다 — 아니면 시험이 실제
    `data/config/service_state.json`(ensure-*.ps1이 읽는 그 파일)을
    건드려 개발 환경에 영향을 준다."""
    calls = []
    monkeypatch.setattr(RS, "_launch_script",
                        lambda name: calls.append(name))
    monkeypatch.setattr(S, "_SERVICE_STATE_FILE",
                        tmp_path / "service_state.json")
    RS._IN_PROGRESS.clear()
    yield calls
    RS._IN_PROGRESS.clear()


def test_정지는_시스템관리자만_가능하다(client, anon_client, seeded_users, login,
                              _no_real_process_control):
    res = client.post("/admin/services/crowd/stop")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert _no_real_process_control == ["stop-crowd-service.ps1"]

    # ⚠️ `client`와 `anon_client`는 같은 객체다(client 픽스처가
    # anon_client를 로그인시켜 그대로 돌려줄 뿐) — 여기서 쿠키를 지우고
    # 끝내면 모듈 범위 `client`를 쓰는 **이후의 모든 시험**이 로그인
    # 안 된 상태로 깨진다. 관제요원으로 확인한 뒤 반드시 관리자로
    # 다시 로그인해 원래 상태로 되돌린다.
    anon_client.cookies.clear()
    login(anon_client, *seeded_users["opr"])
    try:
        res = anon_client.post("/admin/services/crowd/stop")
        assert res.status_code == 403
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])


def test_플랫폼쉘은_정지_대상이_아니다(client, _no_real_process_control):
    res = client.post("/admin/services/main/stop")
    assert res.status_code == 400
    assert _no_real_process_control == []


def test_존재하지_않는_서비스는_거부된다(client, _no_real_process_control):
    res = client.post("/admin/services/no-such-service/stop")
    assert res.status_code == 400
    res = client.post("/admin/services/no-such-service/start")
    assert res.status_code == 400
    assert _no_real_process_control == []


def test_시작은_올바른_스크립트를_부른다(client, _no_real_process_control):
    res = client.post("/admin/services/flood/start")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert _no_real_process_control == ["ensure-flood-service.ps1"]


def test_리눅스에서는_시작이_막힌다(client, monkeypatch, _no_real_process_control):
    from tot_dashboard.core import server_profile as SP
    monkeypatch.setattr(SP, "detect_os", lambda: SP.OS_LINUX)
    res = client.post("/admin/services/traffic/start")
    assert res.status_code == 409
    assert "리눅스" in res.json()["error"]
    assert _no_real_process_control == []


def test_정지_시작이_감사로그에_남는다(client, _no_real_process_control):
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import AuditLog

    client.post("/admin/services/road/stop")
    client.post("/admin/services/road/start")

    db = get_session()
    try:
        actions = [a for (a,) in db.query(AuditLog.action)
                  .filter(AuditLog.target == "road")
                  .order_by(AuditLog.id.desc()).limit(2).all()]
    finally:
        db.close()
    assert RS.SERVICE_START in actions
    assert RS.SERVICE_STOP in actions


def test_상태_폴링_엔드포인트는_collect와_같은_모양이다(client, monkeypatch):
    monkeypatch.setattr(RS, "_check_remote", lambda port: {
        "ok": False, "latency_ms": 5.0, "detail": {"error": "무시(시험)"}})
    res = client.get("/api/admin/services/state")
    assert res.status_code == 200
    body = res.json()
    assert len(body["services"]) == 5
    assert {s["key"] for s in body["services"]} == {
        k for k, *_ in RS.SERVICES}


# ── 정지/시작 API 중복 호출 방지 (2026-09-02 신설) ─────────────────────────
#
# 실기로 겪은 문제: 화면 버튼이 아니라 API를 직접(curl 등으로) 재시도하면
# 같은 키에 대해 정지/시작이 중복 실행될 수 있었다(클라이언트 타임아웃
# 재시도가 서버 쪽에서 이미 진행 중이던 스크립트와 겹쳐 두 번 실행됨).

def test_같은_서비스에_대한_중복_요청은_거부된다(client):
    """★ 첫 요청이 아직 "끝나지 않은" 동안(가짜 프로세스가 신호를 받을
    때까지 wait()에서 안 돌아온다) 같은 키로 두 번째를 보내면 409를
    받아야 한다 — 이게 이번에 막는 바로 그 중복 실행이다."""
    gate = threading.Event()

    class _FakeProc:
        def wait(self, timeout=None):
            if not gate.wait(timeout=timeout):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

    RS._launch_script = lambda name: _FakeProc()
    try:
        res1 = client.post("/admin/services/crowd/stop")
        assert res1.status_code == 200

        res2 = client.post("/admin/services/crowd/stop")
        assert res2.status_code == 409
        assert "진행 중" in res2.json()["error"]

        # 다른 키는 영향받지 않는다 — 잠금은 키 단위다.
        res3 = client.post("/admin/services/road/stop")
        assert res3.status_code == 200
    finally:
        gate.set()  # 감시 스레드를 풀어줘 잠금을 스스로 정리하게 한다
        time.sleep(0.05)
        RS._IN_PROGRESS.clear()


def test_작업이_끝나면_같은_키를_다시_조작할_수_있다(client,
                                        _no_real_process_control):
    """★ 첫 정지가 "끝난"(mock 프로세스가 즉시 wait() 반환) 뒤에는 같은
    키로 다시 조작(시작)할 수 있어야 한다 — 잠금이 계속 남아있으면 안
    된다."""
    res1 = client.post("/admin/services/crowd/stop")
    assert res1.status_code == 200
    time.sleep(0.05)  # 감시 스레드가 잠금을 풀 시간을 준다

    res2 = client.post("/admin/services/crowd/start")
    assert res2.status_code == 200
    assert _no_real_process_control == [
        "stop-crowd-service.ps1", "ensure-crowd-service.ps1"]


# ── 정지 결정이 재기동에도 유지되게 하기 (2026-09-02 신설) ────────────────
#
# 실기로 겪은 사고: 관리자가 화면에서 정지시킨 서비스가, 나중에
# `urbanguard-service.ps1 -Action restart`의 전체 서비스 확인 단계에서
# 관리자의 의도와 무관하게 도로 켜졌다. `admin_stopped_services`가
# DB·`service_state.json` 양쪽에 남아야 `ensure-*.ps1`이 그걸 보고
# 재기동을 건너뛸 수 있다.

def _clear_admin_stopped():
    from tot_dashboard.core.db import get_session
    db = get_session()
    try:
        S.set_admin_stopped_services(db, set())
        db.commit()
    finally:
        db.close()


def test_정지하면_관리자_정지_목록과_파일에_기록된다(client, _no_real_process_control):
    _clear_admin_stopped()
    try:
        res = client.post("/admin/services/road/stop")
        assert res.status_code == 200

        from tot_dashboard.core.db import get_session
        db = get_session()
        try:
            assert "road" in S.admin_stopped_services(db)
        finally:
            db.close()

        assert S._SERVICE_STATE_FILE.exists()
        assert "road" in json.loads(
            S._SERVICE_STATE_FILE.read_text(encoding="utf-8"))["stopped"]
    finally:
        _clear_admin_stopped()


def test_시작하면_관리자_정지_목록과_파일에서_지워진다(client, _no_real_process_control):
    _clear_admin_stopped()
    try:
        client.post("/admin/services/road/stop")
        res = client.post("/admin/services/road/start")
        assert res.status_code == 200

        from tot_dashboard.core.db import get_session
        db = get_session()
        try:
            assert "road" not in S.admin_stopped_services(db)
        finally:
            db.close()

        assert "road" not in json.loads(
            S._SERVICE_STATE_FILE.read_text(encoding="utf-8"))["stopped"]
    finally:
        _clear_admin_stopped()


def test_관리자가_정지시킨_서비스는_화면에_구분_표시된다(client, monkeypatch,
                                          _no_real_process_control):
    """★ "응답 없음"(사고)과 "관리자가 정지함"(의도)을 구분해야 관제요원이
    혼동하지 않는다."""
    _clear_admin_stopped()
    try:
        monkeypatch.setattr(RS, "_check_remote", lambda port: {
            "ok": False, "latency_ms": 5.0, "detail": {"error": "무시(시험)"}})
        client.post("/admin/services/traffic/stop")

        res = client.get("/admin/services")
        assert res.status_code == 200
        assert "관리자가 정지함" in res.text

        from tot_dashboard.core.db import get_session
        db = get_session()
        try:
            services = RS._collect(db)
        finally:
            db.close()
        traffic = next(s for s in services if s["key"] == "traffic")
        assert traffic["admin_stopped"] is True
        road = next(s for s in services if s["key"] == "road")
        assert road["admin_stopped"] is False
    finally:
        _clear_admin_stopped()
