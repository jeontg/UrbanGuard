"""S-61 AI 모델 운영 — 모델 선택과 시험 탐지.

지켜야 할 것.

* **세 도메인 모두 모델을 고를 수 있다** — 침수·인파·노면
* **시험이 운영을 건드리지 않는다** — 결과가 이벤트·노면 현황에 남으면 관제
  화면을 믿을 수 없게 된다. 화면도 그렇게 말해야 한다
* **시스템관리자만 실행한다** — 스트림을 붙들고 CPU를 먹는 작업이다
* **미리보기 경로로 다른 파일을 꺼낼 수 없다** — 파일명만 받는다
* **실행은 감사 로그에 남는다**
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tot_dashboard.core import model_probe as MP
from tot_dashboard.core import model_registry as MR
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import AuditLog
from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    # 수명주기를 켜지 않는다 — main.py 의 runner 스레드는 프로세스당 한 번만
    # start 할 수 있어, 다른 시험 모듈이 뒤이어 열 때 전부 깨진다.
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


# --- 권한 -------------------------------------------------------------------
def test_관제요원은_모델_화면에_들어갈_수_없다(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.get("/models").status_code == 403
        assert anon_client.post("/models/probe", data={
            "domain": "road", "model_key": "x", "target": "y"}).status_code == 403
    finally:
        anon_client.cookies.clear()


# --- 화면 -------------------------------------------------------------------
def test_세_도메인_모두_시험_영역이_나온다(client):
    body = client.get("/models").text
    assert "모델 시험 탐지" in body
    for label in ("침수", "인파", "노면"):
        assert label in body


def test_시험일_뿐이라고_화면이_말한다(client):
    """안 적으면 상시 탐지 모델이 바뀐 줄 안다."""
    body = client.get("/models").text
    assert "상시 탐지에 반영되지도 않습니다" in body
    assert "기록되지 않으며" in body


def test_고를_수_있는_모델이_목록에_실린다(client):
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")
    body = client.get("/models").text
    assert rows[0].key in body


def test_운영_중인_모델은_그렇다고_표시된다(client):
    if not any(m.in_use for m in MR.for_domain("road")):
        pytest.skip("운영 중으로 표시된 노면 모델이 없다")
    assert "운영중" in client.get("/models").text


# --- 실행 -------------------------------------------------------------------
def test_없는_모델을_고르면_400(client):
    res = client.post("/models/probe", data={
        "domain": "road", "model_key": "data/nope/nope.pt", "target": "BLOCK-X"})
    assert res.status_code == 400
    assert "모델을 찾을 수 없습니다" in res.text


def test_알_수_없는_도메인은_400(client):
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")
    res = client.post("/models/probe", data={
        "domain": "weather", "model_key": rows[0].key, "target": "BLOCK-X"})
    assert res.status_code == 400


def test_이미_돌고_있으면_409(client, monkeypatch):
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")

    def busy(*a, **kw):
        raise RuntimeError("다른 시험 탐지가 진행 중입니다.")

    monkeypatch.setattr(MP, "run", busy)
    res = client.post("/models/probe", data={
        "domain": "road", "model_key": rows[0].key, "target": "BLOCK-X"})
    assert res.status_code == 409
    assert "진행 중" in res.text


def test_실행_결과가_화면에_나오고_감사_로그에_남는다(client, monkeypatch):
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")

    def fake(domain, model_key, target, *, kind="cctv", duration_sec=8.0, conf=0.25):
        return MP.ProbeResult(
            domain=domain, model_key=model_key, model_label="시험용 모델",
            backend=MR.YOLO_DET, target_id=target, target_name="시험지점",
            frames_analyzed=5, elapsed_sec=1.2, ok=True,
            metrics={"탐지 건수": 3, "등급": "주의"})

    monkeypatch.setattr(MP, "run", fake)
    res = client.post("/models/probe", data={
        "domain": "road", "model_key": rows[0].key, "target": "BLOCK-X"})
    assert res.status_code == 200
    assert "시험 결과" in res.text
    assert "시험지점" in res.text
    assert "탐지 건수" in res.text

    db = get_session()
    try:
        logs = db.scalars(select(AuditLog).where(
            AuditLog.target.like("모델 시험%"))).all()
        assert logs, "시험 실행이 감사 로그에 남지 않았다"
    finally:
        db.close()


def test_관측_실패는_사유를_그대로_보여_준다(client, monkeypatch):
    """0건과 「못 봤다」를 섞으면 안 본 구간을 점검 완료로 처리하게 된다."""
    rows = MR.for_domain("road")
    if not rows:
        pytest.skip("이 환경에 노면 모델 파일이 없다")

    def fake(domain, model_key, target, **kw):
        return MP.ProbeResult(
            domain=domain, model_key=model_key, model_label="시험용",
            backend=MR.YOLO_DET, target_id=target, target_name="시험지점",
            ok=False, note="연결이 거부되었습니다.")

    monkeypatch.setattr(MP, "run", fake)
    res = client.post("/models/probe", data={
        "domain": "road", "model_key": rows[0].key, "target": "BLOCK-X"})
    assert "연결이 거부되었습니다" in res.text


# --- 미리보기 ---------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "../../../configs/blocks.json",
    "..%2f..%2fmain.py",
    "not-a-uuid.jpg",
    "abcd.png",
])
def test_미리보기로_다른_파일을_꺼낼_수_없다(client, bad):
    assert client.get(f"/models/preview/{bad}").status_code == 404


def test_없는_미리보기는_404(client):
    assert client.get(f"/models/preview/{'a' * 32}.jpg").status_code == 404


def test_저장된_미리보기는_내려받아진다(client, monkeypatch, tmp_path):
    monkeypatch.setattr(MP, "PREVIEW_DIR", tmp_path)
    name = "b" * 32 + ".jpg"
    (tmp_path / name).write_bytes(b"\xff\xd8\xff\xd9")   # 최소 JPEG
    res = client.get(f"/models/preview/{name}")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
