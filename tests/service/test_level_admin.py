"""S-95 위험등급 관리 · S-96 지점 방향 관리 · S-97 상·하류 관리.

지켜야 할 것.

* **침수 어휘가 4등급으로 통일됐다** — 인파와 같은 말을 써야 상황판에서
  어느 쪽이 더 급한지 알 수 있다
* **등급 이름을 바꿔도 판정은 그대로 맞는다** — 비교가 seq 로 되기 때문
* ⚠️ **높은 등급의 문턱이 낮은 등급보다 커야** 한다. 아니면 낮은 등급이
  영원히 안 나온다 — 저장 전에 막는다
* ⚠️ **방향각 빈 칸은 「모른다」다** — 0 으로 바꾸면 전부 북쪽을 보는 것이 되어
  커버리지 분석이 통째로 거짓이 된다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import calibration as CAL
from tot_dashboard.core import relations as R
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (Camera, CameraLink, LevelThreshold,
                                       RiskLevel)
from tot_dashboard.service.main import app

PFX = "TEST-LVL-"


def _purge(db):
    db.execute(sa_delete(CameraLink).where(
        CameraLink.from_camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
    db.commit()


@pytest.fixture
def db(db_schema, seeded_users):
    s = get_session()
    _purge(s)
    # 어휘·구간을 기본값으로 되돌린다. 앞 시험이 고쳐 놓았을 수 있다.
    s.execute(sa_delete(LevelThreshold))
    s.execute(sa_delete(RiskLevel))
    s.commit()
    V.seed_builtin(s)
    s.commit()
    yield s
    _purge(s)
    s.close()


@pytest.fixture
def client(seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["admin"])
    yield c
    c.cookies.clear()


@pytest.fixture
def opr_client(seeded_users, login):
    c = TestClient(app)
    login(c, *seeded_users["opr"])
    yield c
    c.cookies.clear()


# --- 침수 어휘 통일 ---------------------------------------------------------


@pytest.mark.parametrize("cm, expected", [
    (1.0, "관심"),
    (5.0, "주의"),
    (15.0, "경계"),     # 이전엔 「위험」이었다
    (30.0, "심각"),     # 이전엔 「통제 권고」였다
    (60.0, "심각"),
])
def test_침수_단계가_4등급_어휘를_쓴다(cm, expected):
    assert CAL.flood_level(cm) == expected


def test_침수와_인파가_같은_어휘를_쓴다(db):
    """같은 상황판에 서로 다른 등급 이름이 뜨면 안 된다."""
    flood = {CAL.flood_level(c) for c in (1, 5, 15, 30)}
    crowd = {CAL.crowd_level(v) for v in (1, 3, 4, 5)}
    assert flood == crowd == {"관심", "주의", "경계", "심각"}


def test_구간_표를_보면_표가_이긴다(db):
    """표를 고치면 판정이 따라간다."""
    row = db.query(LevelThreshold).filter_by(domain="flood",
                                             level_code="severe").one()
    row.min_value = 100.0
    db.commit()
    # 30cm 는 이제 「심각」이 아니다.
    assert CAL.flood_level(30.0, db=db) == "경계"
    assert CAL.flood_level(100.0, db=db) == "심각"


def test_표를_못_읽으면_코드_기본값으로_되돌아간다(db):
    """⚠️ 실패를 「관심」으로 바꾸면 위험이 안전하게 보인다."""
    db.execute(sa_delete(LevelThreshold))
    db.commit()
    assert CAL.flood_level(30.0, db=db) == "심각"


def test_등급_이름을_바꿔도_구간_판정이_따라온다(db):
    lv = db.get(RiskLevel, "severe")
    lv.label = "위험"
    db.commit()
    assert CAL.flood_level(30.0, db=db) == "위험"
    # ⚠️ seq 는 그대로라 순위 비교는 안 깨진다.
    assert V.rank(db, "위험") == 4


# --- S-95 화면 --------------------------------------------------------------


def test_levels_page_shows_levels_and_thresholds(client, db):
    r = client.get("/settings/levels")
    assert r.status_code == 200
    assert "위험등급 관리" in r.text
    assert "심각" in r.text
    assert "행안부 지하차도 통제 기준" in r.text     # 근거가 화면에 남는다


def test_label_can_be_changed(client, db):
    r = client.post("/settings/levels/label", data={
        "label_interest": "관심", "label_caution": "주의",
        "label_alert": "경계", "label_severe": "위험",
        "color_interest": "", "color_caution": "", "color_alert": "",
        "color_severe": "#c92a2a"})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(RiskLevel, "severe").label == "위험"


def test_empty_label_is_rejected(client, db):
    r = client.post("/settings/levels/label", data={
        "label_interest": "관심", "label_caution": "주의",
        "label_alert": "경계", "label_severe": ""})
    assert r.status_code == 400
    db.expire_all()
    assert db.get(RiskLevel, "severe").label == "심각"


def test_threshold_can_be_changed(client, db):
    # ⚠️ 노면이 들어오면서(2026-08-19) 구간 표가 세 도메인이 됐다.
    #    폼은 **보내지 않은 칸을 건드리지 않으므로** 여기서는 두 도메인만 본다.
    rows = {(r.domain, r.level_code): r.id
            for r in db.query(LevelThreshold).all()
            if r.domain in ("flood", "crowd")}
    data = {f"min_{i}": str(v) for (d, c), i in rows.items()
            for v in [{"caution": 10, "alert": 20, "severe": 40}[c]
                      if d == "flood" else
                      {"caution": 3, "alert": 4, "severe": 5}[c]]}
    r = client.post("/settings/levels/threshold", data=data)
    assert r.status_code == 200
    db.expire_all()
    assert CAL.flood_level(10.0, db=db) == "주의"
    assert CAL.flood_level(40.0, db=db) == "심각"


def test_inverted_thresholds_are_rejected(client, db):
    """⚠️ 높은 등급 문턱이 낮으면 낮은 등급이 영원히 안 나온다."""
    rows = {(r.domain, r.level_code): r.id
            for r in db.query(LevelThreshold).all()
            if r.domain in ("flood", "crowd")}
    data = {}
    for (d, c), i in rows.items():
        if d == "flood":
            data[f"min_{i}"] = {"caution": 30, "alert": 15, "severe": 5}[c]
        else:
            data[f"min_{i}"] = {"caution": 3, "alert": 4, "severe": 5}[c]
    r = client.post("/settings/levels/threshold",
                    data={k: str(v) for k, v in data.items()})
    assert r.status_code == 400
    assert "커야" in r.text
    db.expire_all()
    assert CAL.flood_level(30.0, db=db) == "심각"      # 안 바뀌었다


def test_negative_threshold_is_rejected(client, db):
    row = db.query(LevelThreshold).filter_by(domain="flood",
                                             level_code="severe").one()
    r = client.post("/settings/levels/threshold",
                    data={f"min_{row.id}": "-5"})
    assert r.status_code == 400


def test_levels_requires_permission(opr_client, db):
    assert opr_client.get("/settings/levels").status_code == 403


# --- S-96 지점 방향 관리 ----------------------------------------------------


@pytest.fixture
def cams(db):
    db.add(Camera(id=f"{PFX}A", name="시험지점A", lat=35.1010, lng=129.0300))
    db.add(Camera(id=f"{PFX}B", name="시험지점B", lat=35.1000, lng=129.0300))
    db.commit()
    return db


def test_aim_page_shows_progress(client, cams):
    r = client.get("/settings/aim")
    assert r.status_code == 200
    assert "지점 방향 관리" in r.text
    assert "시험지점A" in r.text


def test_aim_bulk_save(client, cams):
    r = client.post("/settings/aim", data={
        f"bearing_deg_{PFX}A": "90", f"purpose_{PFX}A": "disaster",
        f"bearing_deg_{PFX}B": "270", f"purpose_{PFX}B": "traffic"})
    assert r.status_code == 200
    cams.expire_all()
    assert cams.get(Camera, f"{PFX}A").bearing_deg == 90
    assert cams.get(Camera, f"{PFX}B").purpose == "traffic"


def test_aim_blank_means_unknown(client, cams):
    """⚠️ 빈 칸을 0 으로 바꾸면 전부 북쪽을 보는 것이 된다."""
    client.post("/settings/aim", data={f"bearing_deg_{PFX}A": "90"})
    cams.expire_all()
    assert cams.get(Camera, f"{PFX}A").bearing_deg == 90

    client.post("/settings/aim", data={f"bearing_deg_{PFX}A": ""})
    cams.expire_all()
    assert cams.get(Camera, f"{PFX}A").bearing_deg is None


def test_aim_out_of_range_is_rejected(client, cams):
    r = client.post("/settings/aim", data={f"bearing_deg_{PFX}A": "999"})
    assert r.status_code == 400
    cams.expire_all()
    assert cams.get(Camera, f"{PFX}A").bearing_deg is None


def test_aim_requires_permission(opr_client, cams):
    assert opr_client.get("/settings/aim").status_code == 403


# --- S-97 상·하류 관리 ------------------------------------------------------


def test_flow_page_shows_empty_state(client, cams):
    r = client.get("/settings/flow")
    assert r.status_code == 200
    assert "상·하류 관리" in r.text
    assert "선행 경고가 뜨지 않습니다" in r.text


def test_flow_set_shows_distance(client, cams):
    r = client.post("/settings/flow/set",
                    data={"upper_id": f"{PFX}A", "lower_id": f"{PFX}B"})
    assert r.status_code == 200
    cams.expire_all()
    assert R.downstream_of(cams, f"{PFX}A") == [(f"{PFX}B", 1)]
    # 거리가 화면에 뜬다 — 멀면 잘못 고른 것이다.
    assert "m</td>" in r.text or "111m" in r.text


def test_flow_reverse_is_rejected(client, cams):
    client.post("/settings/flow/set",
                data={"upper_id": f"{PFX}A", "lower_id": f"{PFX}B"})
    r = client.post("/settings/flow/set",
                    data={"upper_id": f"{PFX}B", "lower_id": f"{PFX}A"})
    assert r.status_code == 400
    assert "반대 방향" in r.text


def test_flow_build_preview_writes_nothing(client, cams):
    r = client.post("/settings/flow/build",
                    data={"radius_m": "500", "preview": "1"})
    assert r.status_code == 200
    cams.expire_all()
    assert R.neighbors(cams, f"{PFX}A") == []


def test_flow_build_creates(client, cams):
    r = client.post("/settings/flow/build", data={"radius_m": "500"})
    assert r.status_code == 200
    cams.expire_all()
    assert R.neighbors(cams, f"{PFX}A") == [(f"{PFX}B", 1)]


def test_flow_requires_permission(opr_client, cams):
    assert opr_client.get("/settings/flow").status_code == 403


# --- 메뉴 ------------------------------------------------------------------


def test_menu_has_three_new_screens(client, db):
    r = client.get("/settings/levels")
    for href in ("/settings/levels", "/settings/aim", "/settings/flow"):
        assert href in r.text


# --- 확정된 결정이 화면에 남아 있는가 (2026-08-19) --------------------------
#
# 노면과 침수 단독 5단계는 **그대로 두기로 확정**했다. 왜 여기 없는지를 화면이
# 설명하지 않으면 다음 사람이 「빠뜨렸다」고 보고 다시 손댄다.


def test_levels_page_explains_what_is_excluded(client, db):
    r = client.get("/settings/levels")
    assert "정비 등급" in r.text          # 노면을 뺀 이유
    assert "오버레이" in r.text           # 침수 단독 5단계를 뺀 이유
    assert "확정" in r.text


def test_aim_empty_state_says_it_is_not_broken(client, cams):
    """⚠️ 빈 화면이 고장으로 보이면 안 된다 — 방향각은 향후 적용이다."""
    r = client.get("/settings/aim")
    assert "고장이 아닙니다" in r.text


def test_flow_empty_state_explains_downstream_warning(client, cams):
    """★ 상·하류를 안 넣으면 선행 경고가 안 뜬다는 사실을 알려야 한다.

    모르면 「기능이 없다」로 오해한다.
    """
    r = client.get("/settings/flow")
    assert "고장이 아닙니다" in r.text
    assert "선행 경고" in r.text


# --- 노면 정비 등급을 S-95 에 넣음 (2026-08-19) ------------------------------
#
# 왜 넣었나
#     노면 구간값(1·3·6 건/100m)이 calibration.py 에 상수로 박혀 있어
#     **기관이 화면에서 바꿀 수 없었다.** 침수·인파는 바꿀 수 있는데
#     노면만 못 바꾸는 것은 이유가 없다.
#
# ★ 그런데 **섞으면 안 된다**
#     노면 「긴급」은 「지금 통제하라」가 아니라 「빨리 보수하라」다.
#     위험등급 비교에 끼어들면 **정비 대상이 침수 「심각」과 같은 급으로
#     경보**된다. 아래 시험들이 그 경계를 지킨다.


def test_노면_구간이_화면에_나온다(client, db):
    r = client.get("/settings/levels")
    assert r.status_code == 200
    assert "도로 노면 관리" in r.text
    assert "건/100m" in r.text


def test_노면_특성_설명이_함께_나온다(client, db):
    """★ 숫자만 열어 놓으면 위험등급과 같은 것으로 읽는다."""
    r = client.get("/settings/levels")
    assert "정비 등급" in r.text
    assert "위험등급 아님" in r.text
    assert "언제까지 고쳐야 하는가" in r.text
    # 「미보정 ≠ 양호」를 화면이 말해야 한다.
    assert "「양호」가 아닙니다" in r.text


def test_노면_등급은_위험등급_비교에_끼지_않는다(db):
    """⚠️ 여기서 4가 나오면 노면 「긴급」이 침수 「심각」과 같은 급이 된다."""
    assert V.rank(db, "긴급") == 0
    assert V.rank(db, "road_urgent") == 0
    # 위험등급은 그대로 맞는다.
    assert V.rank(db, "심각") == 4


def test_위험등급_목록에_정비등급이_섞이지_않는다(db):
    codes = {r.code for r in V.active_levels(db)}
    assert "severe" in codes
    assert not any(c.startswith("road_") for c in codes)
    # 정비 등급은 명시해서 부를 때만 나온다.
    maint = {r.code for r in V.active_levels(db, V.KIND_MAINTENANCE)}
    assert maint == {"road_good", "road_watch", "road_repair", "road_urgent"}


def test_노면_구간이_판정에_실제로_쓰인다(db):
    """표를 고치면 판정이 따라 바뀌어야 한다 — 안 그러면 화면이 거짓말이다."""
    assert CAL.road_level(0.5, db=db) == "양호"
    assert CAL.road_level(1.0, db=db) == "관찰"
    assert CAL.road_level(3.0, db=db) == "보수 필요"
    assert CAL.road_level(6.0, db=db) == "긴급"


def test_미보정은_양호가_아니다(db):
    """⚠️ 구간 길이를 안 넣어 판정을 못 한 것이다. 점검이 끝난 게 아니다."""
    assert CAL.road_level(None, db=db) == "미보정"


def test_표를_못_읽어도_판정은_계속된다(db):
    """DB 가 죽어도 노면 판정이 멎으면 안 된다 — 코드 기본값으로 내려간다."""
    assert CAL.road_level(6.0) == "긴급"
    assert CAL.road_level(0.0) == "양호"


def test_노면도_순서_검사를_받는다(client, db):
    """★ 이 시험이 없으면 노면만 검사가 조용히 무력화된 것을 못 잡는다.

    실제로 그랬다 — `level_order` 가 위험등급만 담아서 노면 등급이 전부
    seq 0 이 되고, 값으로만 정렬한 뒤 자기 자신과 비교해 **늘 통과**했다.
    """
    rows = {r.level_code: r.id for r in db.query(LevelThreshold)
            .filter(LevelThreshold.domain == "road").all()}
    # 「긴급」을 「관찰」보다 낮게 넣는다 — 막혀야 한다.
    form = {f"min_{rows['road_watch']}": "5",
            f"min_{rows['road_repair']}": "3",
            f"min_{rows['road_urgent']}": "1"}
    r = client.post("/settings/levels/threshold", data=form)
    assert r.status_code == 400
    assert "크거나 같아야" in r.text


def test_폼에_없는_등급은_건드리지_않는다(client, db):
    """★ 없는 것과 비운 것은 다르다.

    노면 정비 등급이 표에 들어오면서 이름 폼이 그리지 않는 행이 생겼다.
    없는 것을 「비웠다」로 읽어 **이름 변경이 항상 막혔다** — 시험이 잡았다.
    """
    r = client.post("/settings/levels/label", data={
        "label_interest": "관심", "label_caution": "주의",
        "label_alert": "경계", "label_severe": "심각",
        "color_interest": "", "color_caution": "", "color_alert": "",
        "color_severe": "#c92a2a"})
    assert r.status_code == 200
    db.expire_all()
    # 노면 등급 이름이 지워지지 않았다.
    assert db.get(RiskLevel, "road_urgent").label == "긴급"


# --- 침수 위험도 알림 발동 등급 (2026-08-21 flood/traffic 도메인 분리) -------


@pytest.fixture(autouse=True)
def _reset_flood_notify_grade(db):
    """설정 캐시는 프로세스 전역이라 앞 시험이 바꿔 놓은 값이 새어 들어온다.
    DB 행을 지우고 캐시를 비워 매 시험을 기본값(4)에서 시작하게 한다."""
    from sqlalchemy import delete as sa_delete_

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.models import AppSetting

    def _clear():
        db.execute(sa_delete_(AppSetting).where(
            AppSetting.key == ug_settings.KEY_FLOOD_NOTIFY_MIN_GRADE))
        db.commit()
        ug_settings.invalidate()

    _clear()
    yield
    _clear()


def test_기본값은_4등급이다(client, db):
    """개발사 판단값 — 재난 담당부서 확인 전까지의 임시 기본값."""
    import re

    r = client.get("/settings/levels")
    assert r.status_code == 200
    m = re.search(r'<option value="(\d)" selected>', r.text)
    assert m and m.group(1) == "4"


def test_등급을_바꿀_수_있다(client, db):
    from tot_dashboard.core import settings as ug_settings

    r = client.post("/settings/levels/flood-notify", data={"grade": "3"})
    assert r.status_code == 200
    assert "3등급" in r.text
    db.expire_all()
    assert ug_settings.flood_notify_min_grade(db) == 3


def test_범위_밖_값은_거부한다(client, db):
    r = client.post("/settings/levels/flood-notify", data={"grade": "6"})
    assert r.status_code == 400
    assert "1~5" in r.text

    r = client.post("/settings/levels/flood-notify", data={"grade": "0"})
    assert r.status_code == 400


def test_숫자가_아니면_거부한다(client, db):
    r = client.post("/settings/levels/flood-notify", data={"grade": "높음"})
    assert r.status_code == 400
    assert "숫자" in r.text


def test_관제요원은_바꿀_수_없다(opr_client, db):
    """R.SETTINGS_OPS 는 EDIT 권한이 필요하다 — 관제요원(OPR)은 조회만 된다."""
    r = opr_client.post("/settings/levels/flood-notify", data={"grade": "3"})
    assert r.status_code == 403


def test_바뀐_값이_실제_알림_판정에_반영된다(client, db):
    """★ 화면에서 바꾼 값이 코드에 박힌 기본값을 덮어쓰는지 끝까지 확인한다
    — 화면만 바뀌고 실제 판정은 그대로면 관리자가 속게 된다."""
    from types import SimpleNamespace

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.service.runner import _notify_flood_risk

    client.post("/settings/levels/flood-notify", data={"grade": "2"})
    db.expire_all()

    class _FakeNotifier:
        def __init__(self):
            self.calls = []

        def send(self, *, event_key, message, channels):
            self.calls.append(event_key)
            return {"dry_run": True}

    notifier = _FakeNotifier()
    risk = SimpleNamespace(risk_grade=2, risk_score=45.0,
                           grade_label="낮음", top_reason="도로 침수 면적")
    flood_m = SimpleNamespace(timestamp_sec=1.0)
    _notify_flood_risk(notifier, flood_m, risk, "시험지점",
                       min_grade=ug_settings.flood_notify_min_grade())
    assert len(notifier.calls) == 1  # 2등급 기준이면 2등급도 알림이 나가야 한다


# --- 교통·인파·노면 경보 발동 기준 (2026-08-26 신설) -------------------------


@pytest.fixture(autouse=True)
def _reset_domain_notify_thresholds(db):
    """설정 캐시는 프로세스 전역이라 앞 시험이 바꿔 놓은 값이 새어 들어온다.
    세 도메인 모두 기본값에서 시작하게 한다."""
    from sqlalchemy import delete as sa_delete_

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.models import AppSetting

    keys = (ug_settings.KEY_TRAFFIC_NOTIFY_MIN_SEVERITY,
            ug_settings.KEY_CROWD_DENSITY_MIN_SEVERITY,
            ug_settings.KEY_ROAD_EVENT_MIN_GRADE)

    def _clear():
        db.execute(sa_delete_(AppSetting).where(AppSetting.key.in_(keys)))
        db.commit()
        ug_settings.invalidate()

    _clear()
    yield
    _clear()


def test_교통_기본값은_2등급_경계다(client, db):
    r = client.get("/settings/levels")
    assert r.status_code == 200
    from tot_dashboard.core import settings as ug_settings
    assert ug_settings.traffic_notify_min_severity(db) == 2


def test_교통_심각도를_바꿀_수_있다(client, db):
    from tot_dashboard.core import settings as ug_settings

    r = client.post("/settings/levels/traffic-notify", data={"severity": "1"})
    assert r.status_code == 200
    db.expire_all()
    assert ug_settings.traffic_notify_min_severity(db) == 1


def test_교통_범위_밖_값은_거부한다(client, db):
    r = client.post("/settings/levels/traffic-notify", data={"severity": "4"})
    assert r.status_code == 400
    assert "0~3" in r.text


def test_인파_기본값은_3_군중급증위험이다(client, db):
    from tot_dashboard.core import settings as ug_settings
    assert ug_settings.crowd_density_min_severity(db) == 3


def test_인파_심각도를_바꿀_수_있다(client, db):
    from tot_dashboard.core import settings as ug_settings

    r = client.post("/settings/levels/crowd-notify", data={"severity": "2"})
    assert r.status_code == 200
    db.expire_all()
    assert ug_settings.crowd_density_min_severity(db) == 2


def test_인파_범위_밖_값은_거부한다(client, db):
    r = client.post("/settings/levels/crowd-notify", data={"severity": "5"})
    assert r.status_code == 400
    assert "0~4" in r.text


def test_노면_기본값은_1등급_양호다(client, db):
    from tot_dashboard.core import settings as ug_settings
    assert ug_settings.road_event_min_grade(db) == 1


def test_노면_등급을_바꿀_수_있다(client, db):
    from tot_dashboard.core import settings as ug_settings

    r = client.post("/settings/levels/road-notify", data={"grade": "3"})
    assert r.status_code == 200
    db.expire_all()
    assert ug_settings.road_event_min_grade(db) == 3


def test_노면_범위_밖_값은_거부한다(client, db):
    r = client.post("/settings/levels/road-notify", data={"grade": "0"})
    assert r.status_code == 400
    assert "1~4" in r.text


@pytest.mark.parametrize("path,data", [
    ("/settings/levels/traffic-notify", {"severity": "1"}),
    ("/settings/levels/crowd-notify", {"severity": "2"}),
    ("/settings/levels/road-notify", {"grade": "2"}),
])
def test_관제요원은_세_도메인_모두_바꿀_수_없다(opr_client, db, path, data):
    r = opr_client.post(path, data=data)
    assert r.status_code == 403


def test_인파_문턱을_낮추면_낮은_심각도도_이벤트로_올라간다():
    """★ 화면에서 바꾼 값이 실제 이벤트 생성 판정에 반영되는지 끝까지
    확인한다 — 화면만 바뀌고 판정은 그대로면 관리자가 속게 된다.

    ⚠️ 심각도 2(이동흐름혼란)를 쓴다 — 0·1은 「관심」으로 매핑되는데,
    `events.record_detection` 자체가 **도메인 무관 공통 문턱**
    (`EVENT_THRESHOLD="주의"`)을 갖고 있어 「관심」은 이 설정과 무관하게
    이벤트가 되지 않는다. 즉 이 설정이 실제로 여닫을 수 있는 범위는
    2(주의) 이상뿐이다 — 화면 설명에도 이 사실을 적어 둔다."""
    from sqlalchemy import delete as sa_delete

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import AppSetting, Camera, Event
    from tot_dashboard.core.roles import Domain
    from tot_dashboard.service import event_sync as ES

    pfx = "TEST-LVL-CROWD-"
    db = get_session()
    try:
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{pfx}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        db.execute(sa_delete(AppSetting).where(
            AppSetting.key == ug_settings.KEY_CROWD_DENSITY_MIN_SEVERITY))
        db.commit()
        ug_settings.invalidate()

        cam_id = f"{pfx}A"
        db.add(Camera(id=cam_id, name="시험지점", lat=35.1, lng=129.0))
        db.commit()

        # 기본값(3)에서는 심각도 2가 이벤트가 되지 않는다.
        ES._crowd_density_event(db, cam_id, "시험지점", {"severity": 2})
        db.commit()
        assert db.query(Event).filter(Event.block_id == cam_id).count() == 0

        # 문턱을 2로 낮추면 같은 관측이 이벤트가 된다.
        ug_settings.set_crowd_density_min_severity(db, 2)
        db.commit()
        ES._crowd_density_event(db, cam_id, "시험지점", {"severity": 2})
        db.commit()
        rows = db.query(Event).filter(
            Event.block_id == cam_id, Event.domain == Domain.CROWD.value).all()
        assert len(rows) == 1
        assert rows[0].level == "주의"
    finally:
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{pfx}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        db.execute(sa_delete(AppSetting).where(
            AppSetting.key == ug_settings.KEY_CROWD_DENSITY_MIN_SEVERITY))
        db.commit()
        ug_settings.invalidate()
        db.close()


def test_노면_문턱을_올리면_낮은_등급_이벤트가_생기지_않는다():
    from sqlalchemy import delete as sa_delete

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import AppSetting, Camera, Event

    pfx = "TEST-LVL-ROAD-"
    db = get_session()
    try:
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{pfx}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        db.execute(sa_delete(AppSetting).where(
            AppSetting.key == ug_settings.KEY_ROAD_EVENT_MIN_GRADE))
        db.commit()
        ug_settings.invalidate()

        ug_settings.set_road_event_min_grade(db, 3)
        db.commit()

        from tot_dashboard.service import event_sync as ES
        # 등급 1(양호) 손상 — 문턱(3) 미달이라 이벤트가 생기면 안 된다.
        ES.record_road_result({"target": f"{pfx}A", "target_name": "시험구간",
                               "grade": 1, "defects": [{"type": "pothole"}],
                               "frames_analyzed": 5})
        assert db.query(Event).filter(Event.block_id == f"{pfx}A").count() == 0
    finally:
        db.execute(sa_delete(Event).where(Event.block_id.like(f"{pfx}%")))
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        db.execute(sa_delete(AppSetting).where(
            AppSetting.key == ug_settings.KEY_ROAD_EVENT_MIN_GRADE))
        db.commit()
        ug_settings.invalidate()
        db.close()


# --- 강수(rainfall) 데이터 전역 출처 (2026-08-27 신설) -----------------------
#
# 교통위험 실시간 관제가 이미 실제 CCTV(YOLO)로 도는데, 강수량만 39개소
# 전부 합성(sine)이었다 — 카메라별로 하나씩 고치지 않고 화면 설정 하나로
# 바꿀 수 있게 이 스위치를 추가했다(``rainfall_provider.build_rainfall``의
# ``default_type`` 인자 참고).


@pytest.fixture(autouse=True)
def _reset_rainfall_backend(db):
    from sqlalchemy import delete as sa_delete_

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.models import AppSetting

    def _clear():
        db.execute(sa_delete_(AppSetting).where(
            AppSetting.key == ug_settings.KEY_RAINFALL_BACKEND))
        db.commit()
        ug_settings.invalidate()

    _clear()
    yield
    _clear()


def test_강수_출처_기본값은_sine이다(client, db):
    from tot_dashboard.core import settings as ug_settings

    assert ug_settings.rainfall_backend(db) == "sine"
    r = client.get("/settings/levels")
    assert r.status_code == 200
    assert "강수(rainfall) 데이터 출처" in r.text


def test_강수_출처를_kma로_바꿀_수_있다(client, db):
    from tot_dashboard.core import settings as ug_settings

    r = client.post("/settings/levels/rainfall-backend", data={"backend": "kma"})
    assert r.status_code == 200
    db.expire_all()
    assert ug_settings.rainfall_backend(db) == "kma"


def test_알_수_없는_출처는_거부한다(client, db):
    r = client.post("/settings/levels/rainfall-backend",
                    data={"backend": "weather-station"})
    assert r.status_code == 400


def test_kma_서비스키가_없으면_경고를_함께_보여준다(client, db, monkeypatch):
    monkeypatch.delenv("KMA_SERVICE_KEY", raising=False)
    r = client.post("/settings/levels/rainfall-backend", data={"backend": "kma"})
    assert r.status_code == 200
    assert "KMA_SERVICE_KEY" in r.text


def test_kma_서비스키가_있으면_경고가_없다(client, db, monkeypatch):
    monkeypatch.setenv("KMA_SERVICE_KEY", "dummy-key-for-test")
    r = client.post("/settings/levels/rainfall-backend", data={"backend": "kma"})
    assert r.status_code == 200
    assert "KMA_SERVICE_KEY 가 비어" not in r.text


def test_관제요원은_강수_출처를_바꿀_수_없다(opr_client, db):
    r = opr_client.post("/settings/levels/rainfall-backend", data={"backend": "kma"})
    assert r.status_code == 403


def test_담당관리자도_강수_출처는_시스템관리자_전용이라_바꿀_수_없다(
        seeded_users, login, db):
    """★ 침수·교통위험 두 도메인이 함께 쓰는 전역 값이라, 도메인 담당
    관리자(MGR)에게도 열어 주지 않는다(``/settings/flow/build``와 동일
    원칙) — SYS 만 바꿀 수 있어야 한다."""
    from fastapi.testclient import TestClient

    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import User
    from tot_dashboard.core.roles import Domain, Role
    from tot_dashboard.service.main import app

    mgr_id, mgr_pw = "test_mgr_traffic_rainfall", "TestMgrTraffic!2026"
    s = get_session()
    try:
        old = s.query(User).filter(User.login_id == mgr_id).one_or_none()
        if old is not None:
            s.delete(old)
            s.flush()
        create_user(s, login_id=mgr_id, name="교통담당", dept="교통정책과",
                   role=Role.MGR.value, password=mgr_pw,
                   domains=[Domain.TRAFFIC.value], must_change=False)
        s.commit()
    finally:
        s.close()

    c = TestClient(app)
    login(c, mgr_id, mgr_pw)
    try:
        r = c.post("/settings/levels/rainfall-backend", data={"backend": "kma"})
        assert r.status_code == 403
    finally:
        c.cookies.clear()


# --- 인파 상시 카메라별 모니터링 — 검출 소스 (2026-08-27 신설) --------------
#
# "인파관리도 교통위험처럼 CCTV별로 상시 모니터링하는 기능이 있어야 하지
# 않나요?" — 카메라마다 별도 스레드로 상시 관측하는 기능을 신설하며, 사람
# 검출이 무거워(타일 추론, 프레임당 약 1.6초) 전역에서 즉시 되돌릴 수 있는
# 스위치를 함께 뒀다.


@pytest.fixture(autouse=True)
def _reset_crowd_continuous_source(db):
    from sqlalchemy import delete as sa_delete_

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.models import AppSetting

    def _clear():
        db.execute(sa_delete_(AppSetting).where(
            AppSetting.key == ug_settings.KEY_CROWD_CONTINUOUS_SOURCE))
        db.commit()
        ug_settings.invalidate()

    _clear()
    yield
    _clear()


def test_인파_상시_검출_소스_기본값은_detector다(client, db):
    from tot_dashboard.core import settings as ug_settings

    assert ug_settings.crowd_continuous_source(db) == "detector"
    r = client.get("/settings/levels")
    assert r.status_code == 200
    assert "인파 상시 카메라별 모니터링" in r.text


def test_인파_상시_검출_소스를_mock으로_바꿀_수_있다(client, db):
    from tot_dashboard.core import settings as ug_settings

    r = client.post("/settings/levels/crowd-continuous-source", data={"source": "mock"})
    assert r.status_code == 200
    db.expire_all()
    assert ug_settings.crowd_continuous_source(db) == "mock"


def test_인파_상시_검출_소스_알_수_없는_값은_거부한다(client, db):
    r = client.post("/settings/levels/crowd-continuous-source",
                    data={"source": "lidar"})
    assert r.status_code == 400


def test_관제요원은_인파_상시_검출_소스를_바꿀_수_없다(opr_client, db):
    r = opr_client.post("/settings/levels/crowd-continuous-source",
                        data={"source": "mock"})
    assert r.status_code == 403


def test_담당관리자도_인파_상시_검출_소스는_시스템관리자_전용이다(
        seeded_users, login, db):
    from fastapi.testclient import TestClient

    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import User
    from tot_dashboard.core.roles import Domain, Role
    from tot_dashboard.service.main import app

    mgr_id, mgr_pw = "test_mgr_crowd_source", "TestMgrCrowdSrc!2026"
    s = get_session()
    try:
        old = s.query(User).filter(User.login_id == mgr_id).one_or_none()
        if old is not None:
            s.delete(old)
            s.flush()
        create_user(s, login_id=mgr_id, name="인파담당2", dept="안전총괄과",
                   role=Role.MGR.value, password=mgr_pw,
                   domains=[Domain.CROWD.value], must_change=False)
        s.commit()
    finally:
        s.close()

    c = TestClient(app)
    login(c, mgr_id, mgr_pw)
    try:
        r = c.post("/settings/levels/crowd-continuous-source", data={"source": "mock"})
        assert r.status_code == 403
    finally:
        c.cookies.clear()


# --- 인파 검출 타일 격자 (2026-08-29 신설) -----------------------------------
#
# 「4대탐지기능 성능개선 로드맵」 1단계 — 원경 인물 검출력 개선. 화면을
# 몇×몇으로 나눠 각각 검출할지를 전역 설정 하나로 바꿀 수 있게 한다
# (crowd_continuous_source와 같은 원칙 — 카메라 39개소 JSON을 하나씩
# 고치지 않아도 됨).


@pytest.fixture(autouse=True)
def _reset_crowd_tile_grid(db):
    from sqlalchemy import delete as sa_delete_

    from tot_dashboard.core import settings as ug_settings
    from tot_dashboard.core.models import AppSetting

    def _clear():
        db.execute(sa_delete_(AppSetting).where(
            AppSetting.key == ug_settings.KEY_CROWD_TILE_GRID))
        db.commit()
        ug_settings.invalidate()

    _clear()
    yield
    _clear()


def test_인파_타일_격자_기본값은_2x2다(client, db):
    from tot_dashboard.core import settings as ug_settings

    assert ug_settings.crowd_tile_grid(db) == "2x2"
    assert ug_settings.crowd_tile_grid_tuple(db) == (2, 2)
    r = client.get("/settings/levels")
    assert r.status_code == 200
    assert "타일 격자" in r.text


def test_인파_타일_격자를_3x3으로_바꿀_수_있다(client, db):
    from tot_dashboard.core import settings as ug_settings

    r = client.post("/settings/levels/crowd-tile-grid", data={"grid": "3x3"})
    assert r.status_code == 200
    db.expire_all()
    assert ug_settings.crowd_tile_grid(db) == "3x3"
    assert ug_settings.crowd_tile_grid_tuple(db) == (3, 3)


def test_인파_타일_격자_알_수_없는_값은_거부한다(client, db):
    r = client.post("/settings/levels/crowd-tile-grid", data={"grid": "5x5"})
    assert r.status_code == 400


def test_관제요원은_인파_타일_격자를_바꿀_수_없다(opr_client, db):
    r = opr_client.post("/settings/levels/crowd-tile-grid", data={"grid": "3x3"})
    assert r.status_code == 403


def test_담당관리자도_인파_타일_격자는_시스템관리자_전용이다(seeded_users, login, db):
    from fastapi.testclient import TestClient

    from tot_dashboard.core.bootstrap import create_user
    from tot_dashboard.core.db import get_session
    from tot_dashboard.core.models import User
    from tot_dashboard.core.roles import Domain, Role
    from tot_dashboard.service.main import app

    mgr_id, mgr_pw = "test_mgr_tile_grid", "TestMgrTileGrid!2026"
    s = get_session()
    try:
        old = s.query(User).filter(User.login_id == mgr_id).one_or_none()
        if old is not None:
            s.delete(old)
            s.flush()
        create_user(s, login_id=mgr_id, name="인파담당3", dept="안전총괄과",
                   role=Role.MGR.value, password=mgr_pw,
                   domains=[Domain.CROWD.value], must_change=False)
        s.commit()
    finally:
        s.close()

    c = TestClient(app)
    login(c, mgr_id, mgr_pw)
    try:
        r = c.post("/settings/levels/crowd-tile-grid", data={"grid": "3x3"})
        assert r.status_code == 403
    finally:
        c.cookies.clear()
