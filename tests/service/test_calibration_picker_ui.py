# -*- coding: utf-8 -*-
"""ROI 화면(S-81)에 지면 평면 캘리브레이션 점 찍기 UI가 뜨는지 (2026-08-28).

## 왜 이 화면이 필요했나

「4대탐지기능 성능개선 로드맵」 1순위 — ``core/calibration.py``의 지면
평면 함수(실거리 환산·명/㎡·km/h 등)는 이미 구현돼 있었지만, 값을 넣는
화면이 원시 JSON 입력칸뿐이었다(``cameras.html``). 그 결과 39개소 전부
캘리브레이션이 0건이었다(2026-08-28 DB 실측). 이 시험은 ROI 화면에 새로
추가한 클릭 기반 점 찍기 UI가 대상 도메인(침수·교통위험·인파)에서만
뜨고, 저장 폼이 기존에 이미 검증된 백엔드 라우트(``tests/service/
test_calibration_wiring.py``)와 같은 필드명으로 연결돼 있는지 고정한다.
**저장 로직 자체는 이 파일에서 다시 검증하지 않는다** — 그건 이미 다른
파일이 담당한다. 여기는 화면(UI)만 본다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.core import cameras as C
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import Camera
from tot_dashboard.service.main import app

CAM_ID = "CAL-UI-TEST-CAM"


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def camera(db_schema):
    _purge()
    db = get_session()
    try:
        cam, errors = C.create(db, {
            "id": CAM_ID, "name": "캘리브레이션UI시험", "dept": "도로과",
            "lat": 35.1, "lng": 129.0,
            "source_type": "hls", "source_url": "https://example.test/x.m3u8",
        })
        assert not errors, errors
        C.set_domains(db, cam, {
            "flood": {"enabled": True, "continuous": False},
            "traffic": {"enabled": True, "continuous": False},
            "crowd": {"enabled": True, "continuous": False},
            "road": {"enabled": True, "continuous": False},
        })
        db.commit()
    finally:
        db.close()
    yield
    _purge()


def _purge():
    db = get_session()
    try:
        cam = db.get(Camera, CAM_ID)
        if cam is not None:
            db.delete(cam)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("domain", ["flood", "traffic", "crowd"])
def test_지면평면_대상_도메인에는_캘리브레이션_점찍기가_뜬다(client, domain):
    res = client.get(f"/settings/cameras/{CAM_ID}/roi?domain={domain}")
    assert res.status_code == 200
    assert "지면 평면 캘리브레이션" in res.text
    assert f'action="/settings/cameras/{CAM_ID}/calibration"' in res.text
    assert 'name="ground_image_points"' in res.text
    assert 'name="ground_world_points"' in res.text
    assert "미보정" in res.text  # 아직 저장 전이므로 정직하게 미보정


def test_노면_도메인에는_지면평면_캘리브레이션이_안_뜬다(client):
    """노면은 구간 길이(m) 방식이라 지면 평면(호모그래피)을 안 쓴다
    (routes_cameras.py::_calibration_from_form 참고) — 안 쓰는 도메인에
    관계없는 UI를 보여 주면 혼란을 준다.

    ⚠️ ``<h3>`` 제목(가시 영역)만 확인한다 — 같은 문구가 스크립트
    블록의 JS 주석(모든 도메인 공통으로 내려감, 버튼이 없으면 아무 동작도
    안 함)에도 있어, 전체 텍스트 단순 포함 검사는 오탐을 낸다.
    """
    res = client.get(f"/settings/cameras/{CAM_ID}/roi?domain=road")
    assert res.status_code == 200
    assert "<h3>지면 평면 캘리브레이션" not in res.text
    assert 'action="/settings/cameras/{}/calibration"'.format(CAM_ID) not in res.text


def test_저장하면_보정됨으로_바뀐다(client):
    client.post(f"/settings/cameras/{CAM_ID}/calibration", data={
        "domain": "traffic",
        "ground_image_points": "[[0,0],[100,0],[100,100],[0,100]]",
        "ground_world_points": "[[0,0],[10,0],[10,10],[0,10]]"})
    res = client.get(f"/settings/cameras/{CAM_ID}/roi?domain=traffic")
    assert res.status_code == 200
    assert "보정됨" in res.text


def test_한_도메인_저장이_다른_도메인_화면에_안_보인다(client):
    """traffic 만 저장했다면 flood/crowd 화면은 그대로 미보정이어야
    한다 — 도메인마다 독립 저장(test_calibration_wiring.py에서 이미
    검증된 백엔드 동작)이 화면에도 그대로 반영되는지 확인한다."""
    client.post(f"/settings/cameras/{CAM_ID}/calibration", data={
        "domain": "traffic",
        "ground_image_points": "[[0,0],[100,0],[100,100],[0,100]]",
        "ground_world_points": "[[0,0],[10,0],[10,10],[0,10]]"})
    res = client.get(f"/settings/cameras/{CAM_ID}/roi?domain=flood")
    assert res.status_code == 200
    assert "미보정" in res.text


def test_가로세로_길이_참고용_기본값이_채워져_있고_경고문구가_있다(client):
    """★ 2026-08-29 — 사용자 지시("표준값으로 하고 관리자가 수정할 수
    있게")에 따라 가로·세로 입력칸을 빈칸이 아니라 참고용 기본값(3.50)
    으로 미리 채운다. 실측이 아니라는 경고문구가 반드시 같이 떠야
    한다 — 그냥 채워만 두면 실측값으로 착각하고 그대로 저장할 위험이
    있다."""
    res = client.get(f"/settings/cameras/{CAM_ID}/roi?domain=traffic")
    assert res.status_code == 200
    assert 'id="calib-w" step="0.01" min="0.01" value="3.50"' in res.text
    assert 'id="calib-h" step="0.01" min="0.01" value="3.50"' in res.text
    assert "실제로 측정한 값이 아닙니다" in res.text
