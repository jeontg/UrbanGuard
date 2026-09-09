"""End-to-end check of the unified FastAPI service (docs/integration_plan.md
Phase 7): flood/traffic_weather multi-block live risk (from flood3) and
crowd case viewer + notifications (from SAM) served by ONE app.

Uses a synthetic-only blocks.json (TOT_BLOCKS_PATH) so this test never
connects to flood3's live Busan HLS streams.

One shared, module-scoped TestClient is used across all tests: ``main.py``'s
module-level ``runner`` (a ``threading.Thread``) can only be started once per
process — same as the original flood3 app, which assumes a single uvicorn
process lifetime — so each test entering/exiting its own ``TestClient``
context (which runs the FastAPI lifespan startup/shutdown) would call
``runner.start()`` more than once and crash.
"""
import os
from pathlib import Path

import pytest

os.environ["TOT_BLOCKS_PATH"] = str(Path(__file__).parent / "fixtures" / "blocks_synthetic.json")
os.environ.setdefault("NOTIFICATION_DRY_RUN", "true")
os.environ.setdefault("ALERT_RECIPIENTS", "01012345678")

from fastapi.testclient import TestClient  # noqa: E402

from tot_dashboard.service.main import app  # noqa: E402


@pytest.fixture(scope="module")
def anon_client():
    """미인증 클라이언트. 가드가 막는지 확인할 때만 쓴다."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    """시스템관리자로 로그인한 클라이언트.

    인증이 붙은 뒤로는 대부분의 엔드포인트가 로그인을 요구하므로, 기존
    테스트가 쓰던 ``client`` 를 인증된 것으로 바꿨다. 가드 자체의 검증은
    ``anon_client`` 를 쓰는 테스트가 담당한다.

    ``seeded_users`` 가 DB를 요구하므로, PostgreSQL이 없으면 이 픽스처를 쓰는
    테스트는 자동으로 건너뛴다.
    """
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


# --- 인증 가드 (docs/ui_design_spec.md 6절) ---------------------------------
# 아래 세 개는 쿠키가 없어 DB를 건드리지 않으므로 DB 없이도 돈다.
#
# ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 예전에는 대표 보호 API로
# `/api/risk`를 썼다. 침수·교통위험이 별도 프로세스로 옮겨가며 이
# 경로가 main.app에서 사라져(traffic-service 소유), platform-shell에
# 여전히 남아 있는 `/api/blocks`로 바꿨다 — "보호된 API 아무거나"가
# 목적이지 침수·교통 데이터 자체를 보려는 시험이 아니었다.
def test_guard_blocks_unauthenticated_api(anon_client):
    assert anon_client.get("/api/blocks").status_code == 401


def test_guard_blocks_unauthenticated_notification_send(anon_client):
    """무인증 알림 발송 취약점 회귀 방지.

    인증 도입 전에는 이 엔드포인트를 누구나 호출해 주민에게 문자를 보낼 수
    있었다. 다시 열리면 여기서 잡힌다.
    """
    res = anon_client.post("/api/cases/any-case/notifications", json={})
    assert res.status_code == 401


def test_guard_redirects_unauthenticated_page(anon_client):
    res = anon_client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert "/login" in res.headers["location"]


def test_guard_denies_unregistered_api_path_by_default(client):
    """규칙표에 없는 /api 경로는 로그인해도 기본 거부된다(fail-closed)."""
    assert client.get("/api/not-registered-anywhere").status_code == 403


def test_게이트웨이_우회_직접_접속은_게이트웨이로_리다이렉트된다(client):
    """★ 실사용자 제보(2026-09-01) — "CCTV 관리엔 상시로 잡히는데 4개 탐지
    화면엔 아무것도 없다"는 것이 알고 보니 게이트웨이(8080)가 아니라
    platform-shell 포트(보통 8033)로 직접 접속해서 생긴 문제였다.
    `/api/risk`·`/api/flood-risk` 등은 Phase 4로 다른 서비스에 옮겨가
    이 프로세스엔 더 이상 라우트 핸들러가 없는데, RULES 표엔 옛 규칙이
    남아 있어 「규칙 미등록」403까지도 못 가고 FastAPI가 그냥 404를
    내(사용자가 원인을 알 방법이 없었다) 리다이렉트로 고쳤다."""
    for path in ("/api/risk", "/api/flood-risk", "/api/crowd/live",
                "/api/road/blocks"):
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 307, f"{path} → {res.status_code}"
        loc = res.headers["location"]
        assert loc.endswith(path), f"{path} 의 리다이렉트 경로가 안 맞다: {loc}"
        assert ":8080" in loc, f"{path} 가 게이트웨이 포트로 안 갔다: {loc}"


def test_규칙_미등록_403은_옮겨간_경로가_아닐_때만_난다(client):
    """위 리다이렉트가 「규칙 미등록」 403 분기를 아예 집어삼켜 버리진
    않았는지 확인 — 진짜 모르는 경로는 여전히 403이어야 한다."""
    res = client.get("/api/not-registered-anywhere", follow_redirects=False)
    assert res.status_code == 403


def test_operator_cannot_reach_admin_screens(anon_client, seeded_users, login):
    anon_client.cookies.clear()
    login(anon_client, *seeded_users["opr"])
    try:
        assert anon_client.get("/admin/users").status_code == 403
        # 관제요원은 담당 도메인 배정 없이도 전 도메인을 봐야 한다
        # (Phase 4로 /api/risk가 traffic-service로 옮겨가 /api/blocks로 대체)
        assert anon_client.get("/api/blocks").status_code == 200
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])


# --- 비밀번호 변경 -----------------------------------------------------------
def _make_temp_user(login_id: str, password: str):
    """임시 비밀번호 상태(must_change=True)의 계정을 만든다."""
    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import User
    from tot_dashboard.core.roles import Role
    db = get_session()
    try:
        db.query(User).filter(User.login_id == login_id).delete()
        create_user(db, login_id=login_id, name="임시계정", dept="상황실",
                    role=Role.OPR.value, password=password, domains=[])
        db.commit()
    finally:
        db.close()


def test_temp_password_forces_change_before_anything_else(anon_client, login,
                                                          seeded_users):
    """임시 비밀번호로 로그인하면 비밀번호 변경 화면 밖으로 못 나간다.

    발급자(관리자)가 아는 비밀번호가 계속 살아 있으면 안 되기 때문이다.
    """
    _make_temp_user("tmp_user", "TempPass!2026")
    anon_client.cookies.clear()
    login(anon_client, "tmp_user", "TempPass!2026")
    try:
        # 화면은 비밀번호 변경으로 리다이렉트
        res = anon_client.get("/", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"] == "/account/password"
        # API 는 403 (Phase 4로 /api/blocks로 대체 — 위 주석 참고)
        assert anon_client.get("/api/blocks").status_code == 403
        # 변경 화면과 로그아웃은 열려 있어야 한다
        assert anon_client.get("/account/password").status_code == 200

        # 실제로 바꾸면 잠금이 풀린다
        res = anon_client.post("/account/password", data={
            "current_password": "TempPass!2026",
            "new_password": "BrandNew!2026",
            "confirm_password": "BrandNew!2026"}, follow_redirects=False)
        assert res.status_code == 303
        assert anon_client.get("/", follow_redirects=False).status_code == 200
        assert anon_client.get("/api/blocks").status_code == 200
    finally:
        anon_client.cookies.clear()
        login(anon_client, *seeded_users["admin"])


def test_password_change_rejects_wrong_current_password(client):
    res = client.post("/account/password", data={
        "current_password": "definitely-wrong",
        "new_password": "Another!2026x",
        "confirm_password": "Another!2026x"})
    assert res.status_code == 400
    assert "현재 비밀번호" in res.text


def test_password_change_rejects_mismatched_confirmation(client, seeded_users):
    res = client.post("/account/password", data={
        "current_password": seeded_users["admin"][1],
        "new_password": "Another!2026x",
        "confirm_password": "Different!2026x"})
    assert res.status_code == 400
    assert "서로 다릅니다" in res.text


def test_password_change_rejects_weak_password(client, seeded_users):
    res = client.post("/account/password", data={
        "current_password": seeded_users["admin"][1],
        "new_password": "short1", "confirm_password": "short1"})
    assert res.status_code == 400


def test_password_change_rejects_same_as_current(client, seeded_users):
    pw = seeded_users["admin"][1]
    res = client.post("/account/password", data={
        "current_password": pw, "new_password": pw, "confirm_password": pw})
    assert res.status_code == 400
    assert "다른 값" in res.text


# --- 기존 기능 ---------------------------------------------------------------
def test_health_reports_both_domains(anon_client):
    """헬스체크는 인증 없이 열려 있어야 한다(로드밸런서·감시용).

    ⚠️ 2026-08-31 — API 게이트웨이 Phase 1~4(인파·노면·침수·교통위험
    서비스 분리)로 "case_count"·"notifications"·"block_count"·
    "water_segmentation"·"flood_corrupted_skips"·"continuous"는 전부
    해당 서비스 자체 /api/health로 옮겨졌다(test_crowd_service_
    api_smoke.py·test_road_service_api_smoke.py·
    test_flood_service_api_smoke.py·test_traffic_service_api_smoke.py
    참고) — 이 프로세스는 이제 그 어떤 도메인의 실시간 탐지 상태도
    갖지 않는다.
    """
    res = anon_client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert "case_count" not in data
    assert "notifications" not in data
    assert "block_count" not in data
    assert "water_segmentation" not in data
    assert "flood_corrupted_skips" not in data
    assert "continuous" not in data


def test_health_에_ffmpeg_릴레이_가동_현황이_나온다(anon_client):
    """2026-08-30 — ffmpeg 릴레이 8개가 밤새 전부 죽어도 12시간 동안
    아무도 몰랐던 사고 이후 신설. /api/health만 봐도 가동 대수를 바로
    확인할 수 있어야 한다."""
    res = anon_client.get("/api/health")
    data = res.json()
    assert "restream_relay" in data
    assert "running_count" in data["restream_relay"]


def test_health_에_상시_화질_감시_현황이_나온다(anon_client):
    """2026-08-30 — 상시 화질 감시(core/video_quality.py) 신설. 손상
    카메라를 "사용자가 우연히 제보"가 아니라 /api/health만 봐도 알 수
    있어야 한다."""
    res = anon_client.get("/api/health")
    data = res.json()
    assert "video_quality" in data
    assert "grade" in data["video_quality"]
    assert "on_count" in data["video_quality"]


# ⚠️ 2026-08-31(Phase 4) — "침수 손상프레임 건너뛴 횟수"
# (flood_corrupted_skips) 시험은 flood-service로 이관됐다
# (test_flood_service_api_smoke.py 참고) — 이 프로세스의
# /api/health엔 더 이상 그 필드가 없다.


def test_home_dashboard_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "통합 상황판" in res.text
    assert "테스트시" in res.text          # 기관명이 설정값으로 렌더되는지
    # ★ 2026-08-21 flood/traffic 도메인 분리 — 「침수·교통위험」 타일 하나가
    #   「교통위험」·「침수」 두 개로 나뉘었다.
    assert "교통위험" in res.text
    assert "침수" in res.text


def test_domain_dashboard_renders_with_initial_tab(client):
    res = client.get("/road")
    assert res.status_code == 200
    assert '"road"' in res.text            # window.INITIAL_TAB


def test_flood_blocks_endpoint(client):
    """`/api/blocks`는 그대로 platform-shell에 있다(DB 직접 조회, 러너
    무관 — §Phase 4 상세 설계 참고). 실제 위험도 조회(`/api/risk`류)는
    traffic-service로 옮겨져 test_traffic_service_risk_api.py가,
    `/api/flood-risk`류는 flood-service로 옮겨져
    test_flood_service_risk_api.py가 각각 검증한다."""
    res = client.get("/api/blocks")
    assert res.status_code == 200
    block = res.json()["blocks"][0]
    assert block["block_id"] == "BLOCK-TEST"
    # 목록은 S-80 지정과 맞춰야 하므로 상시/선택 구분을 함께 내려준다.
    assert block["mode"] in ("continuous", "selective")


# ⚠️ 2026-08-31 — API 게이트웨이 Phase 1로 인파 사례(케이스) 조회·알림
# (/api/cases*·/media/{case_id}/*)이 crowd_service.py(별도 서비스)로
# 옮겨졌다. 예전 이 자리에 있던 test_crowd_case_list_and_detail_and_media
# ·test_crowd_notification_dry_run_send는
# tests/service/test_crowd_service_cases_api.py로 이관했다.


def test_roi_endpoint_for_the_live_video_overlay(client):
    # BLOCK-TEST (the synthetic test fixture block) has no ROI file -> clear error, not a crash
    missing = client.get("/api/roi/BLOCK-TEST").json()
    assert missing["error"]

    # a real ROI file shipped in configs/roi/ should return actual polygons.
    # ⚠ 2026-08-26 — 응답 형태를 도메인 공통으로 바꿨다(최상위 road_roi 등
    # 침수 전용 키 -> shapes/shape_kinds 로 통일, api_roi() docstring 참고).
    real = client.get("/api/roi/BLOCK-CHORYANG").json()
    assert real["frame_width"] > 0
    assert real["frame_height"] > 0
    assert isinstance(real["shapes"]["road_roi"], list)
    assert real["domain"] == "flood"
    assert real["shape_kinds"]["road_roi"] == "polygon"
    assert real["shape_kinds"]["lane_threshold_line"] == "line"


def test_roi_endpoint_알_수_없는_도메인은_거부한다(client):
    res = client.get("/api/roi/BLOCK-CHORYANG?domain=weather").json()
    assert res["error"]


def test_flood_run_archived_by_standalone_pipeline_is_visible_in_dashboard(client):
    """Reproduces the user-reported gap: running ``tot-flood-standalone
    --video ...`` must produce something visible in the dashboard, with the
    ROI/water/detection overlay actually drawn into the saved video/frames.
    """
    from tot_dashboard.common.case_archive.run_writer import RunWriter
    from tot_dashboard.common.config import PROJECT_ROOT
    from tot_dashboard.flood.standalone_pipeline import Pipeline, process_run

    sample_video = PROJECT_ROOT / "data" / "samples" / "flood" / "underpath_flood1.mp4"
    pipeline = Pipeline.from_config()
    source = {"type": "video", "path": str(sample_video)}
    writer = None
    for _, writer in process_run(pipeline, source, save=True, writer_factory=RunWriter):
        pass
    summary = writer.finalize()

    listing = client.get("/api/flood-runs").json()
    run_ids = [r["run_id"] for r in listing["runs"]]
    assert summary["run_id"] in run_ids

    detail = client.get(f"/api/flood-runs/{summary['run_id']}").json()
    assert detail["video_url"] == f"/media/flood-runs/{summary['run_id']}/processed_video.mp4"
    assert len(detail["metrics"]) == summary["frames"]

    video_res = client.get(detail["video_url"])
    assert video_res.status_code == 200
    assert video_res.headers["content-type"].startswith("video/")

    # path traversal must still be rejected
    escape = client.get(f"/media/flood-runs/{summary['run_id']}/../../../pyproject.toml")
    assert escape.status_code == 404


# ⚠️ 2026-08-31 — API 게이트웨이 Phase 2로 노면관리(S-44 실시간 관제·
# 학습데이터 자동 수집·영상 업로드·관측 이력) API가 road_service.py
# (별도 서비스)로 옮겨졌다. 여기 있던 시험들은 전부
# tests/service/test_road_service_api.py로 이관했다(로직은 바뀌지
# 않음 — 대상 app만 road_service.app으로 바뀜). 다만 홈 화면(`/`)은
# 여전히 이 프로세스(platform-shell)가 서빙하므로, 그 화면을 확인하는
# 시험은 여기 그대로 남긴다.

def test_상황판_노면_카드는_고정값이_아니다(client):
    """예전에는 「개발 중 / 탐지 모델 학습데이터 확보 전」이 박혀 있었다.
    상시 순회가 실제로 도는 지금 그 문구가 남아 있으면 상황판이 거짓말을 한다."""
    html = client.get("/").text
    assert "탐지 모델 학습데이터 확보 전" not in html


# ── 도메인 화면에도 왼쪽 메뉴가 나온다 (2026-08-14) ───────────────────────────
# 예전에는 /flood·/crowd·/road 가 독립 페이지라 메뉴가 사라졌고, 다른 도메인으로
# 가려면 「← 통합 상황판」으로 되돌아가야 했다. 관제 중 화면을 오가는 일이 잦다.

@pytest.mark.parametrize("path,active", [
    ("/flood", "flood-monitor"),
    ("/flood/runs", "flood-runs"),
    ("/crowd", "crowd-monitor"),
    ("/road", "road-status"),
])
def test_도메인_화면에도_왼쪽_메뉴가_나온다(client, path, active):
    html = client.get(path).text
    assert 'class="ug-nav"' in html, f"{path} 에 왼쪽 메뉴가 없습니다"
    assert "통합 상황판 (S-01)" in html
    # 현재 위치가 강조되어야 한다 — 메뉴만 있고 어디인지 모르면 오히려 헷갈린다.
    assert 'aria-current="page"' in html


# ── 노면 현황(S-40)·AI 탐지 실행(S-41) 탭 분리 (2026-08-24) ───────────────────
# 예전에는 /road·/road/detect 가 **같은 탭 하나**를 열어, 메뉴는 둘인데
# 화면은 하나뿐이었다(사용자 지적으로 발견). 이제 서로 다른 탭을 열고,
# 왼쪽 메뉴 강조도 각자 자기 항목에만 붙어야 한다.

def _active_anchor(html: str, href: str) -> str:
    """``href`` 링크의 <a> 태그 전체(속성 포함)를 뽑는다."""
    import re
    m = re.search(rf'<a href="{re.escape(href)}"[^>]*>', html)
    assert m, f"{href} 링크 자체가 없습니다"
    return m.group(0)


def test_노면_현황과_AI_탐지_실행은_서로_다른_탭을_연다(client):
    html_status = client.get("/road").text
    html_detect = client.get("/road/detect").text

    assert 'window.INITIAL_TAB = "road";' in html_status
    assert 'window.INITIAL_TAB = "road-detect";' in html_detect

    # 왼쪽 메뉴 강조도 서로 다른 항목에 붙어야 한다 — 예전엔 둘 다 "노면
    # 현황"에 붙었다(탭이 하나뿐이라 active 계산도 하나였음).
    assert 'aria-current="page"' in _active_anchor(html_status, "/road")
    assert 'aria-current="page"' not in _active_anchor(html_status, "/road/detect")
    assert 'aria-current="page"' in _active_anchor(html_detect, "/road/detect")
    assert 'aria-current="page"' not in _active_anchor(html_detect, "/road")


def _section_html(html: str, section_id: str) -> str:
    import re
    m = re.search(rf'<section id="{re.escape(section_id)}" class="tab-panel">'
                  r'(.*?)</section>', html, re.S)
    assert m, f"<section id=\"{section_id}\"> 를 찾지 못했습니다"
    return m.group(1)


def test_노면_현황_탭에는_AI_탐지_실행_구획이_없다(client):
    """지점별 현황·실시간 관제만 남고, 수동 실행 도구(road-analysis)는
    다른 탭에 있어야 한다."""
    html = client.get("/road").text
    road = _section_html(html, "tab-road")
    detect = _section_html(html, "tab-road-detect")
    assert 'id="road-live"' in road
    assert 'id="road-list"' in road
    assert 'id="road-analysis"' not in road
    assert 'id="road-analysis"' in detect


def test_학습_데이터_수집_도구는_탐지_실행이_아니라_노면_현황에_있다(client):
    """"가진 영상에서 프레임 뽑기"(road-train)는 탐지를 실행하는 기능이
    아니라 학습 데이터를 모으는 기능이다(사용자 지적, 2026-08-24) —
    "AI 탐지 실행" 탭이 아니라 자동 수집 현황과 같은 「노면 현황」 탭에
    있어야 이름과 실제 기능이 일치한다."""
    html = client.get("/road").text
    road = _section_html(html, "tab-road")
    detect = _section_html(html, "tab-road-detect")
    assert 'id="road-train"' in road
    assert 'id="road-train"' not in detect


def test_기존_연계_기능이_탭이_나뉘어도_그대로_로드된다():
    """실시간 관제·목록·AI 탐지 실행 초기화 호출이 화면 분리 후에도 전부 남아
    있어야 한다 — 탭을 나누며 app.js 초기화 호출을 실수로 지우면 두 화면
    모두 깨진 채로 로드된다(HTML은 <script src>로만 불러오므로 정적
    파일 자체를 직접 확인한다)."""
    from tot_dashboard.service.main import BASE_DIR
    js = (BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    for call in ("loadRoadList()", "loadRoadAnalysis()", "loadRoadLive()",
                 "renderRoadUploadForm()"):
        assert call in js, f"{call} 초기화 호출이 빠졌습니다"


def test_도메인_화면이_공통_셸을_쓴다(client):
    """상단바·로고·로그아웃이 도메인 화면에도 있어야 한다."""
    html = client.get("/road").text
    assert 'class="ug-shell"' in html
    assert 'class="ug-topbar"' in html
    assert "로그아웃" in html


def test_main_태그가_중첩되지_않는다(client):
    """셸이 이미 <main> 을 갖고 있다. 중첩되면 유효하지 않은 HTML 이다."""
    html = client.get("/flood").text
    assert html.count("<main") == 1


def test_도메인_탭은_그대로_동작한다(client):
    """셸로 감쌌다고 기존 탭 구조가 사라지면 안 된다."""
    html = client.get("/crowd").text
    for tab in ("flood", "crowd", "runs", "road"):
        assert f'data-tab="{tab}"' in html
    assert '"crowd"' in html          # window.INITIAL_TAB


def test_사이드바_접기_버튼이_있다(client):
    """관제 화면은 지도·영상처럼 가로를 많이 쓴다. 메뉴를 접어 본문을 넓힐 수
    있어야 한다."""
    for path in ("/", "/road", "/crowd"):
        html = client.get(path).text
        assert 'id="ug-rail-btn"' in html, f"{path} 에 접기 버튼이 없습니다"


def test_접기_상태를_그리기_전에_적용한다(client):
    """아래쪽 스크립트에만 맡기면 메뉴가 펴진 채 한 번 그려졌다 접혀 화면이 튄다."""
    html = client.get("/").text
    shell = html.index('id="ug-shell"')
    early = html.index("localStorage.getItem('ug-rail')")
    nav = html.index('class="ug-nav"')
    assert shell < early < nav, "접기 상태 복원이 메뉴보다 뒤에 있습니다"
