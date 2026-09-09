"""AI 모델 학습 화면 (routes_training.py) — 신규, 2026-08-25.

지켜야 할 것.

* **시스템관리자(SYS) 전용** — 사용자 확인(2026-08-25): 학습은 CPU를 몇
  시간 점유해 관제 성능에 영향을 준다. MGR·OPR은 화면·API 모두 403
* **4개 도메인 그룹 + 「운영 설정」에 링크가 모두 있다** — "위험등급 관리"와
  같은 방식(사용자 요청 — 담당자가 자기 도메인에서 바로 찾을 수 있게)
* **동시 1건만 허용** — 실행 중일 때 다른 도메인으로 시작해도 409
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import training_jobs as TJ
from tot_dashboard.core.bootstrap import create_user
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import TrainingRun, User
from tot_dashboard.core.roles import Domain, Role
from tot_dashboard.service.main import app

MGR_ID = "test_mgr_training"
MGR_PW = "TestMgrTraining!2026"


class _FakePopen:
    def __init__(self, *a, **kw):
        self.pid = 999002
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = -15


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _mgr_user(seeded_users):
    db = get_session()
    try:
        old = db.query(User).filter(User.login_id == MGR_ID).one_or_none()
        if old is not None:
            db.delete(old)
            db.flush()
        create_user(db, login_id=MGR_ID, name="설정담당", dept="정보통신과",
                    role=Role.MGR.value, password=MGR_PW,
                    domains=[Domain.FLOOD.value], must_change=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def as_sys(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture()
def as_opr(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["opr"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture()
def as_mgr(anon_client, login):
    login(anon_client, MGR_ID, MGR_PW)
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture()
def db(db_schema):
    s = get_session()
    s.execute(sa_delete(TrainingRun))
    s.commit()
    TJ._PROCS.clear()
    yield s
    s.execute(sa_delete(TrainingRun))
    s.commit()
    TJ._PROCS.clear()
    s.close()


@pytest.fixture()
def flood_dataset_ready(monkeypatch, tmp_path):
    """training_jobs.start()가 실제로 학습을 "시작"하도록 데이터가 있는 척한다."""
    flood_dir = tmp_path / "flood"
    (flood_dir / "images").mkdir(parents=True)
    (flood_dir / "labels").mkdir()
    orig = TJ.resolve_dataset
    monkeypatch.setattr(TJ, "resolve_dataset", lambda key: (
        flood_dir if key == "flood_labeled" else orig(key)))
    monkeypatch.setattr(TJ.subprocess, "Popen", _FakePopen)


# --- 권한 -------------------------------------------------------------------

def test_SYS는_학습_화면을_본다(as_sys, db):
    r = as_sys.get("/settings/training")
    assert r.status_code == 200
    assert "AI 모델 학습" in r.text


def test_MGR은_학습_화면에_403(as_mgr, db):
    r = as_mgr.get("/settings/training")
    assert r.status_code == 403


def test_OPR은_학습_화면에_403(as_opr, db):
    r = as_opr.get("/settings/training")
    assert r.status_code == 403


def test_MGR은_학습_시작_API에_403(as_mgr, db, flood_dataset_ready):
    r = as_mgr.post("/settings/training/start", data={"domain": "flood"})
    assert r.status_code == 403


def test_MGR은_상태_조회_API에_403(as_mgr, db):
    r = as_mgr.get("/settings/training/status?domain=flood")
    assert r.status_code == 403


# --- 메뉴 --------------------------------------------------------------------

def test_SYS_메뉴에_4개_도메인과_운영설정_전부에_학습_링크가_있다(as_sys, db):
    html = as_sys.get("/").text
    assert html.count('href="/settings/training') >= 5, (
        "AI 모델 학습 링크가 침수·교통위험·인파관리·도로 노면 + 운영 설정, "
        "총 5곳에 있어야 한다")


def _is_current(html: str, href: str) -> bool:
    """메뉴 링크 마크업이 href 와 aria-current 를 줄바꿈으로 나눠 렌더링하므로
    (``_layout.html`` — ``<a href="...">`` 다음 줄에 조건부 속성), 공백을
    허용하는 정규식으로 확인한다."""
    import re
    pattern = re.escape(f'href="{href}"') + r'\s+aria-current="page"'
    return re.search(pattern, html) is not None


def test_교통위험_탭을_볼_때는_교통위험_메뉴만_강조된다(as_sys, db):
    """2026-08-25 실사용 중 발견한 결함 — 도메인마다 key 를 공유했더니
    교통위험 탭을 보고 있는데 침수 메뉴 항목까지 함께 강조됐다."""
    html = as_sys.get("/settings/training?domain=traffic").text
    assert _is_current(html, "/settings/training?domain=traffic")
    assert not _is_current(html, "/settings/training?domain=flood")
    assert not _is_current(html, "/settings/training?domain=crowd")
    assert not _is_current(html, "/settings/training?domain=road")


def test_침수_탭을_볼_때는_운영설정_링크도_함께_강조된다(as_sys, db):
    """운영 설정 그룹의 링크는 도메인을 지정하지 않아 기본값(침수)을 연다 —
    그래서 침수 탭을 보고 있을 때는 이 링크도 같은 목적지로 봐야 한다."""
    html = as_sys.get("/settings/training?domain=flood").text
    assert _is_current(html, "/settings/training")
    assert _is_current(html, "/settings/training?domain=flood")


def test_MGR_메뉴에는_학습_링크가_없다(as_mgr, db):
    html = as_mgr.get("/").text
    assert '/settings/training' not in html


# --- 학습 시작·중지 ------------------------------------------------------------

def test_데이터_없는_도메인은_시작이_거부된다(as_sys, db):
    r = as_sys.post("/settings/training/start", data={"domain": "traffic"},
                    follow_redirects=True)
    assert r.status_code == 409
    assert "학습 데이터가 없습니다" in r.text


def test_시작하면_실행중_배너가_뜬다(as_sys, db, flood_dataset_ready):
    r = as_sys.post("/settings/training/start",
                    data={"domain": "flood", "epochs": "5"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "학습을 시작했습니다" in r.text


def test_실행중에_다른_도메인_시작하면_409(as_sys, db, flood_dataset_ready):
    as_sys.post("/settings/training/start", data={"domain": "flood"})
    r = as_sys.post("/settings/training/start", data={"domain": "road"},
                    follow_redirects=True)
    assert r.status_code == 409
    assert "이미 다른 학습이 실행 중입니다" in r.text


def test_다른_도메인_탭에서도_JS_라벨_매핑이_한글로_내려온다(as_sys, db, flood_dataset_ready):
    """2026-08-25 실사용 중 발견 — 교통위험 탭을 보는데 배너가 "road 학습
    실행 중"처럼 원시 키로 떴다(폴링 갱신 JS가 한글 라벨 매핑을 못 받고
    있었음). JS 재갱신용 매핑이 화면에 없으면 몇 초 뒤 원시 키로 바뀐다."""
    as_sys.post("/settings/training/start", data={"domain": "flood"})
    html = as_sys.get("/settings/training?domain=road").text
    # Jinja tojson은 비ASCII를 \uXXXX로 이스케이프하므로(유효한 JS이며
    # 런타임에 "침수"로 해석됨) 그 형태로 확인한다.
    assert '"flood": "\\uce68\\uc218"' in html


def _tag_attrs(html: str, elem_id: str) -> str:
    """id="{elem_id}" 를 가진 첫 태그의 여는 부분(속성 포함) 전체를 돌려준다
    — 공백·줄바꿈에 좌우되지 않고 style 값만 확인하려는 것."""
    import re
    m = re.search(r'<\w+[^>]*\bid="' + re.escape(elem_id) + r'"[^>]*>', html, re.S)
    assert m, f'id="{elem_id}" 태그를 찾지 못했다'
    return m.group(0)


def test_다른_도메인_탭에서는_진행률_한_줄만_보인다(as_sys, db, flood_dataset_ready):
    """2026-08-25 사용자 요청 — 인파관리 탭인데 도로 노면의 전체 학습
    로그가 그대로 보이는 게 어색하다는 지적. 다른 도메인이 실행 중일
    때는 로그 없이 "OOO 도메인에서 학습이 진행 중입니다" 한 줄만 보이고,
    중지 버튼도 숨겨야 한다(그 도메인 탭에 가야 조작할 수 있다)."""
    as_sys.post("/settings/training/start", data={"domain": "flood"})
    html = as_sys.get("/settings/training?domain=road").text
    assert "<b>침수</b> 도메인에서 학습이 진행 중입니다" in html
    # 로그·중지 버튼은 숨겨져 있어야 한다(display:none — 공백은 무시).
    assert "display:none" in _tag_attrs(html, "log-tail")
    assert "display:none" in _tag_attrs(html, "stop-btn")


def test_내_도메인_탭에서는_전체_로그가_보인다(as_sys, db, flood_dataset_ready):
    """대조군 — 내가 시작한 도메인 탭에서는 기존처럼 전체 로그·중지
    버튼이 그대로 보여야 한다(위 시험이 모든 경우를 숨기는 실수를
    하지 않았는지 확인)."""
    as_sys.post("/settings/training/start", data={"domain": "flood"})
    html = as_sys.get("/settings/training?domain=flood").text
    assert "<b>침수</b> 학습 실행 중" in html
    assert "display:none" not in _tag_attrs(html, "log-tail")
    assert "display:none" not in _tag_attrs(html, "stop-btn")


def test_내_도메인_탭에서도_진행률이_함께_나온다(as_sys, db, flood_dataset_ready):
    """2026-08-25 사용자 요청 — 다른 도메인 탭뿐 아니라 내가 보고 있는
    도메인 자체의 학습에도 진행률을 표기해 달라."""
    as_sys.post("/settings/training/start", data={"domain": "flood"})
    run = db.query(TrainingRun).filter(TrainingRun.status == "running").one()
    from pathlib import Path
    Path(run.log_path).write_text(
        "[train] epoch 6/40  loss 0.7  val IoU 0.31  F1 0.46\n", encoding="utf-8")
    html = as_sys.get("/settings/training?domain=flood").text
    assert "<b>침수</b> 학습 실행 중" in html
    assert "진행률 15% (에폭 6/40)" in html  # round(6/40*100) == 15


def test_상태_조회_API가_실행중_정보를_돌려준다(as_sys, db, flood_dataset_ready):
    as_sys.post("/settings/training/start", data={"domain": "flood"})
    r = as_sys.get("/settings/training/status?domain=flood")
    assert r.status_code == 200
    data = r.json()
    assert data["busy"] is not None
    assert data["busy"]["domain"] == "flood"


def test_중지하면_상태가_stopped로_바뀐다(as_sys, db, flood_dataset_ready):
    as_sys.post("/settings/training/start", data={"domain": "flood"})
    run = db.query(TrainingRun).filter(TrainingRun.status == "running").one()
    r = as_sys.post("/settings/training/stop", data={"run_id": str(run.id)},
                    follow_redirects=True)
    assert r.status_code == 200
    db.refresh(run)
    assert run.status == "stopped"
