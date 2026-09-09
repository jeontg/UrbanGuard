"""road-service(``service/road_service.py``)의 S-44 실시간 관제·학습
데이터 자동 수집·영상 업로드·관측 이력 API.

⚠️ 2026-08-31 신설 — API 게이트웨이 Phase 2로 이 API들이
platform-shell(``service/main.py``)에서 이 별도 서비스로 옮겨지면서,
원래 ``tests/service/test_main.py``에 있던 시험들을 그대로 옮겼다
(로직은 바뀌지 않았다 — 대상 app만 ``road_service.app``으로 바뀜, 로그인은
``login_cross_service``로 platform-shell 쿠키를 옮겨 재현).

# ── S-44 노면 실시간 관제 (원본 2026-08-12) ───────────────────────────────────
상시 순회는 지점당 15분 주기라, 결빙·낙하물처럼 분 단위로 변하는 상황에는
대응할 수 없다. 실시간 관제는 「지금 이 지점을 언제 봤는지」와 「집중 감시」를
함께 제공한다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.service.road_service import app


@pytest.fixture(scope="module")
def anon_client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    # /login은 platform-shell에만 있다 — 그쪽에서 로그인해 쿠키만 옮긴다
    # (conftest.py::_do_login_cross_service, 실제 배포의 쿠키 공유와 동일).
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def test_노면_실시간_관제가_지점_목록과_상태를_준다(client):
    d = client.get("/api/road/live").json()
    assert "points" in d and "counts" in d
    assert "continuous" in d and "focus" in d
    # 라우트 순서가 어긋나면 "live" 가 block_id 로 잡혀 지점 조회로 떨어진다.
    assert "error" not in d


def test_실시간_관제는_경과_시간을_함께_준다(client):
    from tot_dashboard.road import results as R
    pts = client.get("/api/road/live").json()["points"]
    if not pts:
        pytest.skip("노면 지정 CCTV가 없어 확인할 수 없습니다")
    R.record(pts[0]["block_id"], {"grade": 1, "grade_label": "정상",
                                  "defects": [], "frames_analyzed": 3})
    try:
        p = next(x for x in client.get("/api/road/live").json()["points"]
                 if x["block_id"] == pts[0]["block_id"])
        assert p["age_sec"] is not None and p["age_sec"] < 10
        assert p["stale"] is False      # 방금 관측했으므로
    finally:
        R.clear()


def test_관측_이력이_없으면_경과_시간을_지어내지_않는다(client):
    from tot_dashboard.road import results as R
    R.clear()
    for p in client.get("/api/road/live").json()["points"]:
        assert p["age_sec"] is None
        assert p["analyzed"] is False and p["stale"] is False


def test_지정되지_않은_지점은_집중_감시할_수_없다(client):
    d = client.post("/api/road/live/focus",
                    json={"camera_id": "존재하지-않는-지점"}).json()
    assert "error" in d


def test_집중_감시_주기는_범위_밖_값을_거부한다(client):
    """주기가 너무 짧으면 상시 순회가 락을 못 잡아 굶는다."""
    res = client.post("/api/road/live/focus",
                      json={"camera_id": "X", "period_sec": 1})
    assert res.status_code == 422


def test_감시_중이_아닐_때_중지해도_오류가_아니다(client):
    d = client.post("/api/road/live/focus/stop").json()
    assert d["ok"] is True


def test_집중_감시_시작은_실행_권한을_요구한다():
    """조회 권한만으로 CPU를 물고 도는 감시를 켤 수 있으면 안 된다.

    규칙표는 위에서부터 먼저 일치하는 것을 쓰므로, 일반 ``/api/road/`` 조회
    규칙보다 **먼저** 등록돼 있어야 한다. 이 규칙표(``core/guard.py``)는
    road-service가 독립적으로 임포트해 그대로 쓴다 — 앱 자체를 띄우지
    않아도 검증 가능하다.
    """
    from tot_dashboard.core import roles as R
    from tot_dashboard.core.guard import RULES

    for methods, pat, resource, action, _domain in RULES:
        if pat.match("/api/road/live/focus") and (methods is None or "POST" in methods):
            assert (resource, action) == (R.DETECT, R.Action.EXECUTE)
            break
    else:
        pytest.fail("집중 감시 경로가 규칙표에 없습니다 — fail-closed 로 403이 됩니다")


def test_실시간_관제_조회는_모니터_권한이면_된다():
    from tot_dashboard.core import roles as R
    from tot_dashboard.core.guard import RULES

    for methods, pat, resource, action, _domain in RULES:
        if pat.match("/api/road/live") and (methods is None or "GET" in methods):
            assert (resource, action) == (R.MONITOR, R.Action.VIEW)
            break
    else:
        pytest.fail("실시간 관제 조회 경로가 규칙표에 없습니다")


# ── 노면 학습 데이터 자동 수집 (원본 Phase 3, 2026-08-12) ─────────────────────

def test_실시간_관제에_수집_현황이_담긴다(client):
    c = client.get("/api/road/live").json()["collect"]
    assert "enabled" in c and "frames" in c and "max_mb" in c


def test_수집은_기본으로_꺼져_있다():
    """도로 영상을 디스크에 쌓는 일은 켜는 것이 운영 판단이어야 한다."""
    from tot_dashboard.core import settings as S
    assert S.DEFAULTS[S.KEY_ROAD_COLLECT] == "off"


def test_수집_전환은_시스템설정_권한을_요구한다():
    """관제 권한만으로 디스크에 영상을 쌓기 시작할 수 있으면 안 된다.

    규칙표는 먼저 일치하는 것을 쓰므로 일반 ``/api/road/`` 규칙보다 앞서야 한다.
    """
    from tot_dashboard.core import roles as R
    from tot_dashboard.core.guard import RULES

    for methods, pat, resource, action, _domain in RULES:
        if pat.match("/api/road/collect") and (methods is None or "POST" in methods):
            assert (resource, action) == (R.SETTINGS_SYS, R.Action.EDIT)
            break
    else:
        pytest.fail("수집 전환 경로가 규칙표에 없습니다")


def test_관제요원은_수집을_켤_수_없다(anon_client, seeded_users, login_cross_service):
    anon_client.cookies.clear()
    login_cross_service(anon_client, *seeded_users["opr"])
    try:
        res = anon_client.post("/api/road/collect", json={"enabled": True})
        assert res.status_code == 403
    finally:
        # ⚠️ ``client`` 픽스처는 이 ``anon_client`` 를 그대로 쓴다(모듈 스코프).
        # 쿠키만 지우고 나가면 **뒤에 오는 테스트가 로그아웃 상태로 돈다.**
        # 관리자 로그인을 되돌려 놓는다.
        anon_client.cookies.clear()
        login_cross_service(anon_client, *seeded_users["admin"])


def test_수집을_켜고_끄면_설정에_남는다(client):
    from tot_dashboard.core import settings as S
    try:
        d = client.post("/api/road/collect", json={"enabled": True}).json()
        assert d["ok"] is True and d["collect"]["enabled"] is True
        # 번호판을 가리지 못한다는 사실을 응답에서 알려야 한다.
        assert "번호판" in d["note"]
    finally:
        client.post("/api/road/collect", json={"enabled": False})
        assert S.get(S.KEY_ROAD_COLLECT) == "off"


# ── 학습 영상 업로드 (원본 2026-08-12) ────────────────────────────────────────

def _tiny_video(tmp_path, frames=30, fps=10.0):
    import cv2
    import numpy as np
    p = tmp_path / "clip.mp4"
    vw = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), fps, (160, 120))
    assert vw.isOpened()
    for i in range(frames):
        vw.write(np.full((120, 160, 3), (i * 8) % 256, dtype=np.uint8))
    vw.release()
    return p


def test_학습_영상_업로드는_시스템설정_권한을_요구한다():
    from tot_dashboard.core import roles as R
    from tot_dashboard.core.guard import RULES

    for methods, pat, resource, action, _domain in RULES:
        if pat.match("/api/road/collect/video") and (methods is None or "POST" in methods):
            assert (resource, action) == (R.SETTINGS_SYS, R.Action.EDIT)
            break
    else:
        pytest.fail("학습 영상 업로드 경로가 규칙표에 없습니다")


def test_동영상이_아니면_거부한다(client):
    res = client.post("/api/road/collect/video",
                      files={"file": ("note.txt", b"hello", "text/plain")},
                      data={"label": "테스트"})
    d = res.json()
    assert d["ok"] is False and "동영상" in d["error"]


def test_영상을_올리면_프레임을_뽑고_원본은_남기지_않는다(client, tmp_path,
                                                        monkeypatch):
    """마스킹하지 않은 도로 영상을 디스크에 쌓는 것이 사전검토서가 지적한
    문제다. 프레임만 가려서 남기고 원본은 지운다."""
    from tot_dashboard.core import image_mask
    from tot_dashboard.core import video_store as VS
    from tot_dashboard.road import dataset_collector as DC

    monkeypatch.setattr(DC, "DATASET_DIR", tmp_path / "raw")
    monkeypatch.setattr(image_mask, "mask_array",
                        lambda img, **kw: (image_mask.NO_TARGET, 0))
    before = set(VS.VIDEO_DIR.glob("*")) if VS.VIDEO_DIR.exists() else set()

    v = _tiny_video(tmp_path)
    with v.open("rb") as f:
        res = client.post("/api/road/collect/video",
                          files={"file": ("clip.mp4", f, "video/mp4")},
                          data={"label": "시험 영상", "interval_sec": "1.0",
                                "max_frames": "3"})
    d = res.json()
    assert d["ok"] is True and d["saved"] == 3
    assert "번호판" in d["note"]
    assert "collect" in d
    after = set(VS.VIDEO_DIR.glob("*")) if VS.VIDEO_DIR.exists() else set()
    assert after == before, f"원본 영상이 남았습니다: {after - before}"


# ── 지점별 관측 이력 (원본 2026-08-14) ────────────────────────────────────────

def test_지점별_관측_이력을_내려준다(client):
    """「지금 상태」만으로는 나빠지고 있는지 알 수 없다."""
    from tot_dashboard.road import results as R
    pts = client.get("/api/road/live").json()["points"]
    if not pts:
        pytest.skip("노면 지정 CCTV가 없어 확인할 수 없습니다")
    cam = pts[0]["block_id"]
    R.clear()
    for n in (0, 1, 3):
        R.record(cam, {"grade": 2, "defects": [{"type": "pothole"}] * n,
                       "frames_analyzed": 4}, source="manual")
    try:
        d = client.get(f"/api/road/history/{cam}").json()
        assert d["camera_id"] == cam
        assert d["count"] >= 3
        # 표는 최근 것부터 읽는 것이 자연스럽다.
        assert d["history"][0]["defect_count"] == 3
        assert d["history"][0]["source"] == "manual"
        assert "persisted" in d
    finally:
        R.clear()


def test_이력_조회는_관제_권한이면_된다():
    from tot_dashboard.core import roles as R
    from tot_dashboard.core.guard import RULES

    for methods, pat, resource, action, _domain in RULES:
        if pat.match("/api/road/history/CAM-A") and (methods is None or "GET" in methods):
            assert (resource, action) == (R.MONITOR, R.Action.VIEW)
            break
    else:
        pytest.fail("이력 조회 경로가 규칙표에 없습니다 — fail-closed 로 403이 됩니다")


def test_관측이_없는_지점도_오류가_아니다(client):
    d = client.get("/api/road/history/없는지점").json()
    assert d["count"] == 0 and d["history"] == []
