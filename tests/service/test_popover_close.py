"""표 안 <details> 팝오버(CCTV 관리의 「탐지 지정」·「보정」·「수정」,
감시지점 관리의 「수정」)의 닫기 기능 (2026-08-27 신고 시정).

**신고받은 증상**: CCTV 관리 화면에서 「탐지 지정」·「보정」·「수정」을
누르면 팝업(정확히는 `<details>` 팝오버)이 뜨는데, 닫는 방법이 summary를
다시 클릭하는 것뿐이었다 — 팝업이 화면을 가리면 원래 summary가 어디
있었는지 찾기 어렵다.

지켜야 할 것.

* **팝오버마다 눈에 보이는 ✕ 버튼이 있어야 한다**
* **바깥을 클릭하거나 Esc를 눌러도 닫혀야 한다** — `_layout.html`의 전역
  스크립트가 `details.ug-popover[open]` 을 전부 대상으로 하므로, 페이지
  종류와 무관하게 확인한다
* **같은 결함이 있던 두 화면(CCTV 관리·감시지점 관리) 모두 확인한다** —
  사용자가 "이 부분뿐 아니라 모든 팝업을 검토해 달라"고 명시적으로 요청
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera
from tot_dashboard.service.main import app

PFX = "TEST-POPOVER-"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def _cam(db_schema):
    db = get_session()
    try:
        cam, errs = C.create(db, {
            "id": f"{PFX}A", "name": "시험지점", "lat": "35.1", "lng": "129.0",
            "source_type": "hls", "source_url": "https://example.test/a.m3u8"})
        assert not errs, errs
        db.commit()
    finally:
        db.close()
    yield
    db = get_session()
    try:
        db.execute(delete(Camera).where(Camera.id == f"{PFX}A"))
        db.commit()
    finally:
        db.close()


def test_전역_팝오버_닫기_스크립트가_모든_화면에_실린다(client):
    """``_layout.html``에서 한 번만 실으므로, 임의의 화면 하나에서만
    확인해도 전체에 적용된다는 뜻이다."""
    html = client.get("/settings/cameras").text
    assert "details.ug-popover[open]" in html
    assert "'Escape'" in html


def test_CCTV_관리_탐지지정_보정_수정_모두_ug_popover_클래스와_닫기버튼을_가진다(client):
    html = client.get("/settings/cameras").text
    assert html.count('class="ug-popover"') >= 3  # 탐지 지정·보정·수정
    assert html.count("ug-popover-x") >= 3
    assert "탐지 지정</strong>" in html
    assert "보정</strong>" in html
    assert "CCTV 정보 수정</strong>" in html


def test_템플릿_settings_blocks도_같은_패턴으로_고쳐져_있다():
    """★ 확인해 보니 ``/settings/blocks``는 ``routes_blocks.py``가
    ``main.py``에 등록돼 있지 않아 **살아있는 경로가 아니다**(404) —
    지금 CCTV 관리 화면은 전부 ``cameras.html``/``routes_cameras.py``다.
    그래도 같은 팝오버 결함이 템플릿 소스에 그대로 남아 있었으므로,
    나중에 이 화면이 다시 연결되더라도 같은 신고가 반복되지 않도록
    소스 파일 자체를 고쳐 둔다. HTTP로는 확인할 수 없어 파일을 직접
    읽어 확인한다."""
    from pathlib import Path

    from tot_dashboard.service import main as _m

    html = (Path(_m.__file__).parent / "templates" / "settings_blocks.html").read_text(
        encoding="utf-8")
    assert 'class="ug-popover"' in html
    assert "ug-popover-x" in html
    assert "지점 정보 수정</strong>" in html


def test_settings_blocks_경로는_아직_등록되지_않은_죽은_코드다(client):
    """★ 이 시험이 실패하면(즉 경로가 살아났으면) 위 시험을 HTTP 기반으로
    되돌려야 한다는 신호다.

    ⚠️ 반드시 **로그인한** client로 확인해야 한다 — 비로그인 상태로는
    ``AuthGuard`` 미들웨어가 라우팅 이전에 ``/settings/*`` 전부를 로그인
    화면으로 리다이렉트해 버려(303), 경로가 실제로 등록됐는지와 무관하게
    항상 200(로그인 화면)이 나온다. 이 함정 때문에 처음엔 죽은 코드가
    아닌 것으로 착각할 뻔했다."""
    r = client.get("/settings/blocks")
    assert r.status_code == 404


def test_닫기버튼은_details를_직접_닫는다(client):
    """``this.closest('details').removeAttribute('open')`` — 자바스크립트
    엔진 없이 문자열로만 확인하지만, 표시 방식이 바뀌어도(팝오버 개수가
    늘어도) 매 버튼이 자기 자신이 속한 details만 닫도록 하는 패턴인지
    확인한다."""
    html = client.get("/settings/cameras").text
    assert "this.closest('details').removeAttribute('open')" in html


# --- 팝업 테두리 구분 (2026-08-27, 같은 날 후속 신고) ------------------------
#
# "어두운 배경에서 팝업 테두리가 구분되지 않는다"는 신고 — 팝오버가 뜬
# 상자 자체(``.ug-popover-panel``)가 ``--border-strong``(패널과의 밝기
# 차이가 일반 ``--border``보다 훨씬 큰 값, core/settings.py::derive_theme
# 참고)을 쓰는지 확인한다.


def test_팝오버_상자는_강한_경계선_클래스를_쓴다(client):
    html = client.get("/settings/cameras").text
    assert html.count("ug-popover-panel") >= 3  # 탐지 지정·보정·수정


def test_강한_경계선_변수가_화면에_실려온다(client):
    """``--border-strong``이 실제로 :root 에 내려오는지 — 값이 없으면
    ``.ug-popover-panel``의 ``var(--border-strong)``이 무효가 되어 조용히
    아무 테두리도 안 그려진다."""
    html = client.get("/settings/cameras").text
    assert "--border-strong:" in html
