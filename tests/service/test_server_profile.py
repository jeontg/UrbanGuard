"""서버 운영 — 운영체제·재기동 방식 (S-87) 과 S-01 재기동 연동.

지켜야 할 것.

* **OS 만으로 재기동 방식이 정해지지 않는다** — 감시 방식을 따로 받는다
* **OS 에 맞지 않는 조합은 저장되지 않는다** — 리눅스에 윈도우 서비스를 고르면
  거부한다. 화면이 막아도 요청은 직접 보낼 수 있다
* **「확인 불가」와 「감시 없음」을 구분한다** — 섞으면 화면이 거짓말을 한다
* **설정과 실제가 다르면 막는다** — 안내문이 엉뚱한 로그를 가리키게 된다
* **「감시 확인」은 필요한 방식에만 쓴다** — systemd 처럼 확실한 증거가 있는
  방식까지 확인을 받으면, 확인이 형식이 되고 정작 필요한 곳에서 무뎌진다
* **설정을 못 읽으면 열어 주지 않는다**
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import server_profile as SP
from tot_dashboard.core import service_control as SC
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AppSetting, AuditLog
from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def clean(db_schema, monkeypatch):
    # 시험이 실제 감시 환경을 흉내 내지 않도록 표시를 모두 지운다.
    for k in (SP.SYSTEMD_ENV, SP.WINDOWS_ENV, SC.SUPERVISED_ENV):
        monkeypatch.delenv(k, raising=False)
    SC._pending.clear()
    _purge()
    yield
    SC._pending.clear()
    _purge()


def _purge():
    s = get_session()
    try:
        s.execute(sa_delete(AppSetting))
        s.execute(sa_delete(AuditLog))
        s.commit()
    finally:
        s.close()
    S.invalidate()


# --- OS × 방식 조합 ----------------------------------------------------------

def test_OS_마다_고를_수_있는_방식이_다르다():
    assert SP.ST_SYSTEMD in SP.STRATEGIES_BY_OS[SP.OS_LINUX]
    assert SP.ST_NSSM not in SP.STRATEGIES_BY_OS[SP.OS_LINUX]
    assert SP.ST_NSSM in SP.STRATEGIES_BY_OS[SP.OS_WINDOWS]
    assert SP.ST_SYSTEMD not in SP.STRATEGIES_BY_OS[SP.OS_WINDOWS]
    # 감시 스크립트는 양쪽 공통이다.
    for o in (SP.OS_LINUX, SP.OS_WINDOWS):
        assert SP.ST_SUPERVISOR in SP.STRATEGIES_BY_OS[o]


def test_맞지_않는_조합은_재기동_불가로_떨어진다():
    """리눅스에서 윈도우 서비스로 재기동하려다 조용히 죽는 것보다,
    「불가」라고 말하는 편이 낫다."""
    eff_os, eff = SP.normalize(SP.OS_LINUX, SP.ST_NSSM)
    assert eff_os == SP.OS_LINUX and eff == SP.ST_NONE


def test_auto_는_실제_OS로_풀린다():
    eff_os, _ = SP.normalize(SP.OS_AUTO, SP.ST_SUPERVISOR)
    assert eff_os == SP.detect_os()


# --- 감지 -------------------------------------------------------------------

def test_아무_표시도_없으면_확인_불가이지_감시_없음이_아니다():
    assert SP.detect_strategy() is None


def test_systemd_는_INVOCATION_ID_로_확인한다(monkeypatch):
    monkeypatch.setenv(SP.SYSTEMD_ENV, "abc123")
    assert SP.detect_strategy() == SP.ST_SYSTEMD


def test_윈도우_서비스는_등록시_넣은_표시로만_확인한다(monkeypatch):
    monkeypatch.setenv(SP.WINDOWS_ENV, "1")
    assert SP.detect_strategy() == SP.ST_NSSM


def test_감시_스크립트는_serve_py_표시로_확인한다(monkeypatch):
    monkeypatch.setenv(SC.SUPERVISED_ENV, "1")
    assert SP.detect_strategy() == SP.ST_SUPERVISOR


# --- 재기동 가능 판정 ---------------------------------------------------------

def test_감지되면_가능하다(monkeypatch):
    monkeypatch.setenv(SP.SYSTEMD_ENV, "x")
    p = SP.profile(os_choice=SP.OS_LINUX, strategy=SP.ST_SYSTEMD)
    assert p["can_restart"] is True and p["reason"] == ""


def test_설정과_실제가_다르면_막는다(monkeypatch):
    """systemd 로 설정했는데 실제로는 serve.py 아래면, 안내문이 엉뚱한
    로그(journalctl)를 가리키게 된다."""
    monkeypatch.setenv(SC.SUPERVISED_ENV, "1")
    p = SP.profile(os_choice=SP.OS_LINUX, strategy=SP.ST_SYSTEMD)
    assert p["can_restart"] is False
    assert "실제로는" in p["reason"]


def test_확인_불가일_때는_관리자_확인이_있어야_열린다():
    p = SP.profile(os_choice=SP.OS_WINDOWS, strategy=SP.ST_NSSM)
    assert p["can_restart"] is False and "확인할 수 없습니다" in p["reason"]
    p2 = SP.profile(os_choice=SP.OS_WINDOWS, strategy=SP.ST_NSSM, ack=True)
    assert p2["can_restart"] is True


def test_방식이_없음이면_막는다():
    p = SP.profile(os_choice=SP.OS_LINUX, strategy=SP.ST_NONE)
    assert p["can_restart"] is False and "없음" in p["reason"]


def test_OS_불일치는_알리되_막지는_않는다(monkeypatch):
    """납품 대상 서버를 미리 설정해 두는 일이 실제로 있다."""
    other = SP.OS_LINUX if SP.detect_os() == SP.OS_WINDOWS else SP.OS_WINDOWS
    strategy = SP.STRATEGIES_BY_OS[other][0]
    if strategy == SP.ST_SYSTEMD:
        monkeypatch.setenv(SP.SYSTEMD_ENV, "x")
    else:
        monkeypatch.setenv(SP.WINDOWS_ENV, "1")
    p = SP.profile(os_choice=other, strategy=strategy)
    assert p["os_mismatch"] is True
    assert p["can_restart"] is True     # 알리기만 한다


def test_안내문이_방식마다_다르다():
    lin = SP.guide_for(SP.ST_SYSTEMD, "urbanguard")
    win = SP.guide_for(SP.ST_NSSM, "UrbanGuard")
    assert "journalctl -u urbanguard" in lin["log"]
    assert "이벤트 뷰어" in win["log"]
    assert "Restart=always" in lin["setup"]
    assert "nssm" in win["setup"]
    # 종료 코드 42 를 막으면 안 된다는 경고가 systemd 안내에 있어야 한다.
    assert "42" in lin["setup"]


def test_설정을_못_읽으면_열어_주지_않는다(monkeypatch):
    monkeypatch.delenv(SC.SUPERVISED_ENV, raising=False)
    ok, why = SC.can_restart(None)
    assert ok is False and "되살릴 것이 없습니다" in why


# --- 화면 -------------------------------------------------------------------

def test_설정_화면이_열리고_설정과_실제를_함께_보여_준다(client):
    r = client.get("/settings/server")
    assert r.status_code == 200
    assert "서버 운영" in r.text
    assert "운영체제" in r.text and "재기동 방식" in r.text
    # 두 OS 의 등록 방법이 모두 실려 있어야 한다 — 납품 대상이 양쪽이다.
    assert "journalctl" in r.text and "이벤트 뷰어" in r.text


def test_저장하면_설정과_감사로그가_남는다(client):
    r = client.post("/settings/server",
                    data={"server_os": "linux",
                          "restart_strategy": "systemd",
                          "service_name": "urbanguard"})
    assert r.status_code == 200

    db = get_session()
    try:
        assert S.get(S.KEY_SERVER_OS, db) == "linux"
        assert S.get(S.KEY_RESTART_STRATEGY, db) == "systemd"
        assert S.get(S.KEY_SERVICE_NAME, db) == "urbanguard"
        acts = db.scalars(__import__("sqlalchemy").select(AuditLog.action)).all()
        assert "settings.update" in acts or len(acts) >= 1
    finally:
        db.close()


def test_OS에_맞지_않는_조합은_400(client):
    r = client.post("/settings/server",
                    data={"server_os": "linux", "restart_strategy": "nssm"})
    assert r.status_code == 400
    assert "쓸 수 없습니다" in r.text


def test_감시_확인은_필요한_방식에만_저장된다(client):
    """관리자가 지금 보고 있는 폼에 표시한 것이므로, 같은 제출에서 방식을
    바꿨더라도 새 방식에 대한 확인으로 본다. 다만 프로그램이 스스로 확인할 수
    있는 방식(systemd·serve.py)에는 이 표시를 쓰지 않는다 — 필요 없는 곳까지
    확인을 받으면 확인이 형식이 되고 정작 필요한 곳에서 무뎌진다."""
    r0 = client.post("/settings/server",
                     data={"server_os": "windows", "restart_strategy": "nssm",
                           "service_name": "UrbanGuard",
                           "supervision_ack": "1"})
    assert r0.status_code == 200
    db = get_session()
    try:
        assert S.get(S.KEY_SUPERVISION_ACK, db) == "1"
    finally:
        db.close()

    # 확인이 필요 없는 방식으로 바꾸면, 값이 와도 눕힌다.
    r = client.post("/settings/server",
                    data={"server_os": "windows",
                          "restart_strategy": "supervisor",
                          "supervision_ack": "1"})
    assert r.status_code == 200
    db = get_session()
    try:
        assert S.get(S.KEY_SUPERVISION_ACK, db) == "0"
    finally:
        db.close()
    assert "쓰지 않습니다" in r.text


def test_관제요원은_서버_설정에_못_들어간다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        r = anon_client.get("/settings/server", follow_redirects=False)
        assert r.status_code in (302, 303, 403)
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])


def test_설정이_재기동_API에_반영된다(client, monkeypatch):
    """S-87 에서 정한 방식이 S-01 재기동 판정을 실제로 좌우한다."""
    client.post("/settings/server",
                data={"server_os": "linux", "restart_strategy": "systemd",
                      "service_name": "urbanguard"})
    # systemd 로 설정했지만 그 아래가 아니므로 막혀야 한다. systemd 는
    # 표시가 없으면 감시도 없다고 **단정할 수 있는** 방식이라, 「확인 불가」가
    # 아니라 「그 아래가 아니다」로 말해야 한다.
    r = client.post("/api/service/restart")
    assert r.status_code == 409
    assert "돌고 있지 않습니다" in r.json()["error"]

    # 실제로 systemd 아래라면 통과한다(죽이지는 않는다).
    monkeypatch.setenv(SP.SYSTEMD_ENV, "abc")
    monkeypatch.setattr(SC, "request_restart", lambda shutdown, **kw: True)
    r2 = client.post("/api/service/restart")
    assert r2.status_code == 200 and r2.json()["ok"] is True

    db = get_session()
    try:
        rows = db.scalars(__import__("sqlalchemy").select(AuditLog)).all()
        hit = [r for r in rows if r.action == "service.restart"]
        assert hit and hit[-1].after["strategy"] == "systemd"
    finally:
        db.close()


# --- 성능(서비스별 스레드 상한, 2026-09-02 신설 — 이후 사용자 요청으로
#     서비스마다 다른 값을 지정하도록 재설계) --------------------------------
#
# ⚠️ `S._SERVICE_THREADS_FILE`을 매 시험 임시 경로로 바꿔치기한다 — 아니면
# 시험이 실제 `data/config/perf.json`(scripts/serve.py가 읽는 그 파일)을
# 건드려 개발 환경에 영향을 준다.

@pytest.fixture(autouse=True)
def _isolate_perf_file(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_SERVICE_THREADS_FILE", tmp_path / "perf.json")


def test_성능_카드에_코어_수와_권장값이_표시된다(client):
    r = client.get("/settings/server")
    assert r.status_code == 200
    assert "논리 코어 수" in r.text
    import os as _os
    cpu = _os.cpu_count() or 4
    assert f"{cpu}개" in r.text
    assert str(max(1, cpu // 4)) in r.text
    # 서비스별 행이 전부 보여야 한다.
    for label in ("플랫폼-쉘", "인파관리", "노면관리", "침수", "교통위험"):
        assert label in r.text


def test_서비스별_스레드_상한을_저장하면_DB와_파일_모두_기록된다(client):
    r = client.post("/settings/server/performance",
                    data={"threads_crowd": "3", "threads_traffic": "6"})
    assert r.status_code == 200
    assert "다음 서비스 재기동부터 적용됩니다" in r.text

    db = get_session()
    try:
        assert S.service_thread_overrides(db) == {"crowd": 3, "traffic": 6}
        acts = db.scalars(__import__("sqlalchemy").select(AuditLog.action)).all()
        assert "settings.update" in acts
    finally:
        db.close()

    assert json.loads(S._SERVICE_THREADS_FILE.read_text(encoding="utf-8")) == {
        "crowd": 3, "traffic": 6}
    # 값을 지정하지 않은 서비스(road)는 그대로 자동값을 쓴다.
    assert "road" not in S.service_thread_overrides(db)


def test_0_이하나_숫자가_아니면_서비스별로_거부한다(client):
    r = client.post("/settings/server/performance", data={"threads_crowd": "0"})
    assert r.status_code == 400
    assert "인파관리" in r.text
    r2 = client.post("/settings/server/performance", data={"threads_road": "-1"})
    assert r2.status_code == 400
    r3 = client.post("/settings/server/performance", data={"threads_flood": "abc"})
    assert r3.status_code == 400


def test_코어_수를_넘는_합계도_저장되지만_초과_표시가_뜬다(client):
    """★ 2026-09-02 사용자 결정 — 코어 수 상한을 막지 않는다. 대신
    "5개 서비스가 전부 뜰 때의 합계"를 관리자가 인지할 수 있게 "초과"
    표시를 지속적으로 보여준다."""
    cpu = __import__("os").cpu_count() or 4
    # 서비스 하나만으로도 코어 수를 넘도록 크게 잡는다.
    over = cpu + 1000
    r = client.post("/settings/server/performance",
                    data={"threads_traffic": str(over)})
    assert r.status_code == 200
    assert "초과" in r.text or "이득" in r.text

    db = get_session()
    try:
        assert S.service_thread_overrides(db)["traffic"] == over
    finally:
        db.close()

    # 새로고침(별도 요청)해도 "초과" 표시가 계속 남아 있어야 한다 —
    # 저장 직후 메시지 한 번이 아니라 상시 표시다.
    page = client.get("/settings/server")
    assert "초과" in page.text


def test_전부_자동값이어도_합계가_코어_수를_넘으면_초과_표시가_뜬다(client):
    """★ 권장값(코어 수 ÷ 4)을 5개 서비스에 그대로 곱하면 코어 수를
    넘는 기계가 실제로 있다(예: 16코어 → 4×5=20). 아무것도 지정하지
    않은 "전부 자동" 상태에서도 이 사실을 보여줘야 한다."""
    cpu = __import__("os").cpu_count() or 4
    recommended = max(1, cpu // 4)
    page = client.get("/settings/server")
    assert page.status_code == 200
    if recommended * 5 > cpu:
        assert "초과" in page.text


def test_빈_값으로_저장하면_그_서비스만_자동으로_돌아간다(client):
    client.post("/settings/server/performance",
               data={"threads_crowd": "3", "threads_road": "2"})
    r = client.post("/settings/server/performance",
                    data={"threads_crowd": "", "threads_road": "2"})
    assert r.status_code == 200

    db = get_session()
    try:
        overrides = S.service_thread_overrides(db)
    finally:
        db.close()
    assert "crowd" not in overrides
    assert overrides["road"] == 2
    assert json.loads(S._SERVICE_THREADS_FILE.read_text(encoding="utf-8")) == {
        "road": 2}


def test_관제요원은_성능_설정을_저장할_수_없다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        r = anon_client.post("/settings/server/performance",
                             data={"threads_crowd": "2"})
        assert r.status_code == 403
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])
