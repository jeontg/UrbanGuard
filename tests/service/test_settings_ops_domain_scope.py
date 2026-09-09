"""SETTINGS_OPS 도메인 스코프 (2026-08-22 전수점검에서 발견한 결함 수정).

**신고받은 문제.** ``core/roles.py`` 가 ``SETTINGS_OPS``(CCTV·ROI·위험등급·
알림규칙 등)를 ``DOMAIN_SCOPED`` 로 선언해 뒀는데, 실제로 이 자원을 쓰는
``routes_cameras.py``·``routes_levels.py``·``routes_config.py`` 의 모든
라우트가 ``domain=`` 을 넘기지 않아 **부서담당자(MGR)가 담당하지 않는
도메인의 설정까지 조회·수정할 수 있었다.** 인파 담당 MGR이 침수·교통·도로
카메라 등록, ROI, 위험도 임계값, 알림 규칙을 그대로 열람·수정할 수 있는
상태였다.

이 파일은 그 수정이 실제로 걸리는지 확인한다 — **전수점검 전에는 MGR 역할
계정을 만드는 테스트가 이 저장소 전체에 단 하나도 없었다**(실제로
`grep -rn 'Role.MGR.value' tests/` 결과 0건). 그래서 이 결함이 그동안
드러나지 않았다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import cameras as C
from tot_dashboard.core.bootstrap import create_user
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera, CameraDomain, CameraRoi, User
from tot_dashboard.core.roles import Domain, Role
from tot_dashboard.service.main import app

CROWD_MGR_ID = "test_mgr_crowd"
CROWD_MGR_PW = "TestMgrCrowd!2026"

FLOOD_CAM = "SCOPE-FLOOD-1"
CROWD_CAM = "SCOPE-CROWD-1"
SHARED_CAM = "SCOPE-SHARED-1"  # flood + crowd 둘 다


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _mgr_user(seeded_users):
    """인파(crowd)만 담당하는 부서담당자 계정을 만든다."""
    db = get_session()
    try:
        old = db.query(User).filter(User.login_id == CROWD_MGR_ID).one_or_none()
        if old is not None:
            db.delete(old)
            db.flush()
        create_user(db, login_id=CROWD_MGR_ID, name="인파담당", dept="안전총괄과",
                    role=Role.MGR.value, password=CROWD_MGR_PW,
                    domains=[Domain.CROWD.value], must_change=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _cams(db_schema):
    """침수전용/인파전용/공유 카메라 3개를 심고, 테스트 후 지운다."""
    db = get_session()
    try:
        for cid, doms in (
            (FLOOD_CAM, {"flood": True}),
            (CROWD_CAM, {"crowd": True}),
            (SHARED_CAM, {"flood": True, "crowd": True}),
        ):
            cam, errs = C.create(db, {"id": cid, "name": f"{cid}지점",
                                      "source_type": "hls",
                                      "source_url": "https://example.test/a.m3u8",
                                      "lat": "35.1", "lng": "129.0"})
            assert not errs, errs
            sel = {d.value: {"enabled": doms.get(d.value, False), "continuous": False}
                  for d in Domain}
            C.set_domains(db, cam, sel)
        db.commit()
    finally:
        db.close()
    yield
    db = get_session()
    try:
        for cid in (FLOOD_CAM, CROWD_CAM, SHARED_CAM):
            cam = C.get(db, cid)
            if cam is not None:
                db.execute(sa_delete(CameraRoi).where(CameraRoi.camera_id == cid))
                db.execute(sa_delete(CameraDomain).where(CameraDomain.camera_id == cid))
                db.execute(sa_delete(Camera).where(Camera.id == cid))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def as_sys(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture()
def as_crowd_mgr(anon_client, login):
    login(anon_client, CROWD_MGR_ID, CROWD_MGR_PW)
    yield anon_client
    anon_client.cookies.clear()


# --- 목록: 담당 도메인과 겹치는 카메라만 보인다 ------------------------------

def test_MGR은_담당_도메인_카메라만_목록에서_본다(as_crowd_mgr):
    body = as_crowd_mgr.get("/settings/cameras").text
    assert CROWD_CAM in body
    assert SHARED_CAM in body  # crowd 가 겹치므로 보임
    assert FLOOD_CAM not in body


def test_SYS는_전부_본다(as_sys):
    body = as_sys.get("/settings/cameras").text
    assert FLOOD_CAM in body
    assert CROWD_CAM in body
    assert SHARED_CAM in body


# --- 메타데이터 수정: 하나라도 겹쳐야 한다 -----------------------------------

def test_MGR은_담당_아닌_카메라_수정이_403(as_crowd_mgr):
    r = as_crowd_mgr.post(f"/settings/cameras/{FLOOD_CAM}/update",
                          data={"name": "바꿔치기"})
    assert r.status_code == 403


def test_MGR은_담당_카메라는_수정_가능(as_crowd_mgr):
    r = as_crowd_mgr.post(f"/settings/cameras/{CROWD_CAM}/update",
                          data={"name": "새이름", "source_type": "hls",
                                "source_url": "https://example.test/a.m3u8",
                                "lat": "35.1", "lng": "129.0"})
    assert r.status_code == 200


def test_MGR은_공유_카메라도_수정_가능(as_crowd_mgr):
    """하나라도 담당이면 메타데이터는 고칠 수 있다(any-overlap)."""
    r = as_crowd_mgr.post(f"/settings/cameras/{SHARED_CAM}/update",
                          data={"name": "공유이름", "source_type": "hls",
                                "source_url": "https://example.test/a.m3u8",
                                "lat": "35.1", "lng": "129.0"})
    assert r.status_code == 200


# --- 도메인 지정 변경: 바뀌는 도메인만 담당이면 된다 -------------------------
#
# ⚠️ 브라우저 체크박스는 **꺼져 있으면 폼에서 아예 빠진다** — 빈 문자열로
# 보내면 안 된다(``_selections()`` 는 "키가 있는가" 로 판정하므로, 빈
# 문자열도 "있다"로 잡혀 실수로 켠 것과 같아진다).

def test_MGR은_담당_아닌_도메인_지정을_못_바꾼다(as_crowd_mgr):
    """공유 카메라(flood=True, crowd=True)에서 flood 를 끄려 하면 막힌다."""
    r = as_crowd_mgr.post(f"/settings/cameras/{SHARED_CAM}/domains",
                          data={"use_crowd": "on"})  # use_flood 생략 = 끄기 시도
    assert r.status_code == 403


def test_MGR은_담당_도메인_지정은_바꿀_수_있다(as_crowd_mgr):
    """flood 는 켠 채로 두고(그대로) crowd 만 끄면 통과한다."""
    r = as_crowd_mgr.post(f"/settings/cameras/{SHARED_CAM}/domains",
                          data={"use_flood": "on"})  # use_crowd 생략 = 끄기
    assert r.status_code == 200


# --- ROI: 도메인이 URL 에 명시적이다 -----------------------------------------

def test_MGR은_담당_아닌_도메인_ROI_저장이_403(as_crowd_mgr):
    r = as_crowd_mgr.post(f"/settings/cameras/{FLOOD_CAM}/roi/flood",
                          json={"frame_width": 640, "frame_height": 360,
                                "shapes": {"road_roi": [[[0, 0], [1, 0], [1, 1]]]}})
    assert r.status_code == 403


def test_MGR은_담당_도메인_ROI는_저장_가능(as_crowd_mgr):
    r = as_crowd_mgr.post(f"/settings/cameras/{CROWD_CAM}/roi/crowd",
                          json={"frame_width": 640, "frame_height": 360,
                                "shapes": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_MGR은_담당_아닌_도메인_ROI_화면_조회도_403(as_crowd_mgr):
    r = as_crowd_mgr.get(f"/settings/cameras/{FLOOD_CAM}/roi")
    assert r.status_code == 403


# --- 삭제: 카메라가 걸친 도메인 전부가 담당이어야 한다 -----------------------

def test_MGR은_공유_카메라를_못_지운다(as_crowd_mgr):
    """다른 부서(flood)도 쓰는 카메라라 혼자 지울 수 없다."""
    r = as_crowd_mgr.post(f"/settings/cameras/{SHARED_CAM}/delete")
    assert r.status_code == 403
    db = get_session()
    try:
        assert C.get(db, SHARED_CAM) is not None  # 안 지워졌다
    finally:
        db.close()


def test_MGR은_전담_카메라는_지울_수_있다(as_crowd_mgr):
    r = as_crowd_mgr.post(f"/settings/cameras/{CROWD_CAM}/delete")
    assert r.status_code == 200
    db = get_session()
    try:
        assert C.get(db, CROWD_CAM) is None
    finally:
        db.close()
    # 뒤 테스트에 영향 주지 않도록 되살린다.
    db = get_session()
    try:
        cam, errs = C.create(db, {"id": CROWD_CAM, "name": f"{CROWD_CAM}지점",
                                  "source_type": "hls",
                                  "source_url": "https://example.test/a.m3u8",
                                  "lat": "35.1", "lng": "129.0"})
        assert not errs, errs
        C.set_domains(db, cam, {"crowd": {"enabled": True, "continuous": False}})
        db.commit()
    finally:
        db.close()


# --- 전역 일괄 작업: 시스템관리자 전용 --------------------------------------

def test_MGR은_지역_일괄_해지를_못_한다(as_crowd_mgr):
    r = as_crowd_mgr.post("/settings/cameras/region/delete",
                          data={"region": "none", "confirm": "지역 미상"})
    assert r.status_code == 403


def test_MGR은_인접관계_재계산을_못_한다(as_crowd_mgr):
    r = as_crowd_mgr.post("/settings/cameras/links/build",
                          data={"radius_m": "500", "preview": "1"})
    assert r.status_code == 403


# --- 위험등급 관리(routes_levels.py) ----------------------------------------

def test_MGR은_등급_이름_색은_못_고친다(as_crowd_mgr):
    """RiskLevel 은 도메인 구분이 없는 전역 어휘라 시스템관리자 전용."""
    r = as_crowd_mgr.post("/settings/levels/label", data={})
    assert r.status_code == 403


def test_MGR은_담당_아닌_도메인_알림_임계값을_못_바꾼다(as_crowd_mgr):
    r = as_crowd_mgr.post("/settings/levels/flood-notify", data={"grade": "3"})
    assert r.status_code == 403


def test_MGR은_담당_도메인_알림_임계값은_바꿀_수_있다(as_crowd_mgr):
    r = as_crowd_mgr.post("/settings/levels/crowd-notify", data={"severity": "3"})
    assert r.status_code == 200


def test_SYS는_어떤_도메인_알림_임계값도_바꿀_수_있다(as_sys):
    r = as_sys.post("/settings/levels/flood-notify", data={"grade": "4"})
    assert r.status_code == 200


# --- routes_config.py — 위험도 임계값(S-82)·알림규칙(S-83)은 침수 전용 ------

def test_MGR은_위험도_임계값_화면에_403(as_crowd_mgr):
    r = as_crowd_mgr.get("/settings/threshold")
    assert r.status_code == 403


def test_MGR은_알림규칙_화면에_403(as_crowd_mgr):
    r = as_crowd_mgr.get("/settings/alert")
    assert r.status_code == 403


def test_SYS는_위험도_임계값_화면을_본다(as_sys):
    r = as_sys.get("/settings/threshold")
    assert r.status_code == 200


# --- routes_config.py — 교통위험 판정 임계값(2026-08-24 신설)은 교통 전용 --

def test_MGR은_교통위험_판정_임계값_화면에_403(as_crowd_mgr):
    """인파 담당은 교통 전용 화면을 못 본다 — 위험도 임계값(S-82)과 같은
    도메인 스코프(SETTINGS_OPS + domain=TRAFFIC)를 쓴다."""
    r = as_crowd_mgr.get("/settings/traffic")
    assert r.status_code == 403


def test_SYS는_교통위험_판정_임계값_화면을_본다(as_sys):
    r = as_sys.get("/settings/traffic")
    assert r.status_code == 200
    assert "교통위험 판정 임계값" in r.text


# --- 왼쪽 메뉴 재분류 (2026-08-24) --------------------------------------------
#
# "설정"(12항목)·"관리자"(6항목) 두 그룹에 뭉쳐 있던 것을 8/24 오전에 실제
# 라우트 권한에 맞춰 6개 그룹으로 나눴었으나, 쪼갠 만큼 묶음 헤더가 늘어
# 오히려 세로가 길어졌다(사용자 지적). 8/24 오후 재정리로 성격이 비슷한
# 것끼리 3개 그룹(운영 설정/기록·감사/시스템 관리)으로 다시 통합했다 —
# `docs/202608241437/menu_reorganization_asis_tobe.md` 참고. 도메인 전용
# 설정(위험도 임계값 등)은 여전히 해당 도메인 그룹 안에 있고, 여러
# 도메인이 함께 쓰는 화면(위험등급 관리)도 관련 도메인 그룹에 링크가
# 그대로 있다. 이 시험은 그 구조와 도메인 스코프가 실제 화면에
# 반영됐는지 확인한다.

def test_SYS는_새로_통합된_설정_그룹을_전부_본다(as_sys):
    html = as_sys.get("/").text
    for label in ("운영 설정", "기록·감사", "시스템 관리"):
        assert label in html, f"'{label}' 그룹이 안 보인다"
    # 예전의 뭉뚱그린 그룹 이름과, 8/24 오전에 썼다가 다시 합친 6개 그룹
    # 이름은 더 이상 메뉴 헤더로 남아 있으면 안 된다(그룹 라벨은
    # <span>아이콘</span>라벨</p> 구조라, "라벨</p>" 형태로 찾으면 부분
    # 문자열이 겹치는 다른 그룹과 혼동되지 않는다).
    assert ">설정</p>" not in html
    assert ">관리자</p>" not in html
    for old_label in ("카메라·지점 관리", "SOP 관리", "AI 모델 관리",
                      "시스템·기관 관리", "계정·감사", "오류·증거 관리"):
        assert f">{old_label}</p>" not in html, f"'{old_label}'은 이제 없어야 한다"


def test_위험도_임계값은_이제_침수_그룹_안에_있다(as_sys):
    """도메인 전용 설정(S-82·S-83)이 "설정" 대분류가 아니라 그 도메인
    그룹 안에서 바로 보여야 한다 — 사용자가 "이름은 일반적인데 실제로는
    침수 전용"이라 헷갈리던 문제의 핵심 수정."""
    html = as_sys.get("/").text
    assert '"/settings/threshold"' in html
    assert '"/settings/alert"' in html
    assert '"/settings/traffic"' in html


def test_위험등급_관리는_인파_노면_교통위험_그룹에도_링크가_있다(as_sys):
    """위험등급 관리(S-95)는 침수·인파·노면 3개 도메인 공용 화면이라,
    운영 설정 그룹(구 카메라·지점 관리)뿐 아니라 인파관리·도로 노면
    관리·교통위험 그룹에도 같은 링크가 나타나야 한다(사용자 요청 —
    각 도메인에서 자기 설정을 찾을 수 있게)."""
    html = as_sys.get("/").text
    assert html.count('href="/settings/levels"') >= 4, (
        "위험등급 관리 링크가 운영 설정 + 인파·노면·교통위험 "
        "3개 도메인 그룹, 총 4곳에 있어야 한다")


def test_MGR은_담당_아닌_도메인의_전용_설정_링크를_못_본다(as_crowd_mgr):
    """인파만 담당하는 MGR 은 침수 전용(S-82/S-83)·교통 전용(신규 화면)
    링크가 메뉴에 아예 안 보여야 한다 — 링크가 보이는데 눌러야 403이
    나는 것보다, 애초에 안 보이는 편이 낫다(기존 메뉴 설계 원칙)."""
    html = as_crowd_mgr.get("/").text
    assert 'href="/settings/threshold"' not in html
    assert 'href="/settings/alert"' not in html
    assert 'href="/settings/traffic"' not in html
    # 자기 도메인(인파) 링크는 그대로 보여야 한다.
    assert 'href="/settings/levels"' in html


# --- ROI 저장 시 진단 경고 (2026-08-23) --------------------------------------
#
# 저장 검증은 「꼭짓점 3개 이상」만 본다. 2026-08-22부터 ROI 가 실제 판정에
# 쓰이므로, 그 위에서 한 번 더 훑어 **막지 않고 알린다**.

def test_수상한_ROI는_저장되되_경고가_함께_온다(as_sys):
    """손톱만 한 구역 — 손이 미끄러졌을 수 있다. 막지는 않는다."""
    r = as_sys.post(f"/settings/cameras/{CROWD_CAM}/roi/crowd",
                    json={"frame_width": 1000, "frame_height": 1000,
                          "shapes": {"analysis_roi": [
                              [[10, 10], [40, 10], [40, 40], [10, 40]]]}})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, "수상하다고 저장을 막으면 정당한 설정도 못 한다"
    assert any("만 덮습니다" in w for w in body.get("warnings", [])), body


def test_멀쩡한_ROI에는_경고가_없다(as_sys):
    r = as_sys.post(f"/settings/cameras/{CROWD_CAM}/roi/crowd",
                    json={"frame_width": 1000, "frame_height": 1000,
                          "shapes": {"analysis_roi": [
                              [[100, 100], [600, 100], [600, 600], [100, 600]]]}})
    assert r.status_code == 200
    assert r.json().get("warnings") == []


def test_꼬인_ROI는_저장돼도_경고한다(as_sys):
    """화면에서 본 모양과 실제 판정 영역이 달라지는 상태."""
    r = as_sys.post(f"/settings/cameras/{CROWD_CAM}/roi/crowd",
                    json={"frame_width": 1000, "frame_height": 1000,
                          "shapes": {"analysis_roi": [
                              [[100, 100], [400, 400], [400, 100], [100, 400]]]}})
    assert r.status_code == 200
    assert any("꼬였" in w for w in r.json().get("warnings", []))
