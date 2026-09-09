"""S-88 증거 자료 — 도메인 표시·팝업 확인·이벤트 지점 표시.

## 왜 이 시험들이 있나

전체 점검 뒤 요청받은 세 가지다.

1. **이벤트가 침수·인파·노면 중 무엇인지** 알 수 있어야 한다 —
   `EventEvidence.domain` 은 수집 때부터 채워졌는데 **화면에 없었다.**
   관리자가 확인하려면 이벤트 상세로 들어가야 했다
2. **팝업에서 바로 확인**하고 필요할 때만 내려받는다 — 예전에는 클립이
   **곧바로 내려받기**라 보려고 받은 파일이 디스크에 방치됐다. 영상은
   개인정보다
3. **이벤트가 난 지점을 표시**한다

## ⚠️ ③을 정직하게 답하는 것이 이 시험들의 알맹이다

**2026-08-20 부터 탐지 상자(bbox)를 실제로 저장한다** — 침수는 물 영역
경계, 인파는 이벤트를 일으킨 트랙의 추적 상자, 노면은 손상 상자.
**지어내지 않는다** — 그 틱에 못 찾았거나 그 이전에 수집된 자료는 상자가
없고, 화면은 「없다」고 정직하게 말해야 한다. 그 밖에 두 가지를 더 준다.

* 클립의 **이벤트 순간**(앞 구간 10초 뒤) — 이건 확실히 안다
* 그 지점·도메인의 **감시 구역(ROI)** — 「여기서 났다」가 아니라
  **「이 구역을 보고 있었다」**는 뜻이고, 화면이 그 차이를 말해야 한다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import evidence as EV
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraRoi, Event, EventEvidence
from tot_dashboard.service.main import app

PFX = "TEST-EVP-"


def _purge():
    db = get_session()
    try:
        db.execute(sa_delete(EventEvidence).where(
            EventEvidence.camera_id.like(f"{PFX}%")))
        db.execute(sa_delete(CameraRoi).where(
            CameraRoi.camera_id.like(f"{PFX}%")))
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(db_schema):
    _purge()
    yield
    _purge()


@pytest.fixture(scope="module")
def client(seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


def _make(domain="flood", kind=EV.KIND_CLIP, *, with_roi=True,
         boxes=None, frame_w=0, frame_h=0) -> int:
    db = get_session()
    try:
        cam = Camera(id=f"{PFX}A", name="시험지점", lat=35.1, lng=129.0)
        db.add(cam)
        if with_roi:
            db.add(CameraRoi(camera_id=f"{PFX}A", domain=domain,
                             frame_width=1920, frame_height=1080,
                             shapes={"road_roi": [[0, 0], [10, 0], [10, 10]]}))
        ev = Event(domain=domain, block_id=f"{PFX}A", place_name="시험지점",
                   event_type="시험", level="경계", peak_level="경계",
                   status="open")
        db.add(ev)
        db.flush()
        row = EventEvidence(event_id=ev.id, camera_id=f"{PFX}A", domain=domain,
                            level="경계", kind=kind, path="x/y.mp4",
                            bytes=1, sha256="a" * 64,
                            duration_sec=20 if kind == EV.KIND_CLIP else 0,
                            boxes=boxes, frame_w=(frame_w or None),
                            frame_h=(frame_h or None))
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# --- ① 도메인 표시 ----------------------------------------------------------


def test_목록에_도메인이_보인다(client):
    _make(domain="crowd")
    r = client.get("/admin/evidence")
    assert r.status_code == 200
    assert "인파" in r.text
    assert "도메인" in r.text


def test_도메인으로_거를_수_있다(client):
    _make(domain="flood")
    r = client.get("/admin/evidence?domain=road")
    assert r.status_code == 200
    # 노면만 걸렀으니 방금 만든 침수 자료는 안 나온다.
    assert f"{PFX}A" not in r.text


# --- ② 팝업 확인 ------------------------------------------------------------


def test_보기는_팝업을_연다(client):
    """★ 예전에는 클립이 **곧바로 내려받기**였다."""
    _make(kind=EV.KIND_CLIP)
    r = client.get("/admin/evidence")
    assert "evOpen(" in r.text
    assert 'id="ev-modal"' in r.text


def test_inline_은_내려받기가_아니다(client):
    """`filename=` 을 주면 브라우저가 받아 버린다 — 팝업에서 못 본다."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"
           / "service" / "routes_evidence.py").read_text(encoding="utf-8")
    assert "if inline:" in src
    assert "EVIDENCE_VIEW" in src


# --- ③ 이벤트 지점 표시 -----------------------------------------------------


def test_클립은_이벤트_순간을_알려준다(client):
    ev_id = _make(kind=EV.KIND_CLIP)
    d = client.get(f"/api/evidence/{ev_id}/meta").json()
    # 앞 구간(PRE_SEC) 뒤가 이벤트 순간이다.
    assert d["event_at_sec"] == EV.PRE_SEC
    assert d["duration_sec"] == 20


def test_정지영상은_이벤트_순간이_0이다(client):
    ev_id = _make(kind=EV.KIND_IMAGE)
    d = client.get(f"/api/evidence/{ev_id}/meta").json()
    assert d["event_at_sec"] == 0


def test_ROI_를_함께_준다(client):
    ev_id = _make(domain="flood", with_roi=True)
    d = client.get(f"/api/evidence/{ev_id}/meta").json()
    assert d["roi"] is not None
    assert d["roi"]["frame_width"] == 1920
    assert d["roi"]["shapes"]["road_roi"]


def test_ROI_가_없으면_없다고_준다(client):
    """⚠️ 없는 것을 그린 척하면 안 된다."""
    ev_id = _make(with_roi=False)
    d = client.get(f"/api/evidence/{ev_id}/meta").json()
    assert d["roi"] is None


def test_상자가_없으면_없다고_밝힌다(client):
    """★ 없는 것은 없다고 말한다 — 2026-08-20 이전 자료가 이 경우다."""
    ev_id = _make()
    d = client.get(f"/api/evidence/{ev_id}/meta").json()
    assert d["has_box"] is False
    assert d["boxes"] == []


def test_저장된_상자를_그대로_돌려준다(client):
    """★ 2026-08-20 부터 실제 관측값을 저장한다 — 있으면 있다고 답한다."""
    boxes = [{"x1": 10, "y1": 20, "x2": 30, "y2": 40, "label": "침수 영역"}]
    ev_id = _make(domain="flood", boxes=boxes, frame_w=640, frame_h=480)
    d = client.get(f"/api/evidence/{ev_id}/meta").json()
    assert d["has_box"] is True
    assert d["boxes"] == boxes
    assert d["frame_w"] == 640
    assert d["frame_h"] == 480


def test_화면이_상자의_뜻을_도메인별로_설명한다(client):
    """⚠️ 상자를 그냥 그리기만 하면 「여기서 났다」로 오독된다."""
    boxes = [{"x1": 1, "y1": 1, "x2": 2, "y2": 2, "label": "물"}]
    _make(domain="flood", boxes=boxes, frame_w=100, frame_h=100)
    r = client.get("/admin/evidence")
    assert "세그멘테이션이 실제로 물이라고 판정한 영역" in r.text
    assert "이 이벤트를 일으킨 그 사람의 실제 추적 상자" in r.text
    assert "모델이 실제로 찾은 손상 위치" in r.text


def test_화면이_ROI_의_뜻을_설명한다(client):
    """⚠️ 「여기서 났다」로 읽히면 안 된다."""
    _make()
    r = client.get("/admin/evidence")
    assert "이 구역을 보고 있었다" in r.text
    assert "저장된 탐지 상자가 없습니다" in r.text


def test_구형_클립_코덱_오류_안내가_있다(client):
    """★ 2026-08-19 이전 클립(mp4v)은 이 환경 브라우저에서 재생되지 않는다
    (2026-08-20 실측). 검은 화면만 보이면 고장으로 오해한다.

    ⚠️ 근거 주석에 에러 코드가 있는 것은 괜찮다 — **관제요원에게 보여줄
    안내 문구**에 코드가 아니라 사람이 읽을 설명이 들어 있는지만 본다.
    """
    r = client.get("/admin/evidence")
    assert "예전 형식(mp4v)이라 이 화면에서" in r.text
    # ⚠️ 실제 문자열엔 <b>내려받기」</b>로 태그가 끼어 있다 — 부분 문자열
    # 두 개로 나눠 확인한다.
    assert "내려받기" in r.text and "다른 재생 프로그램으로" in r.text


def test_없는_자료는_404(client):
    assert client.get("/api/evidence/999999/meta").status_code == 404
