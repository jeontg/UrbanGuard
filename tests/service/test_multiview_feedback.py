"""S-05 멀티뷰 관제 · S-07 탐지 피드백 — 화면과 라우트.

지켜야 할 것.

* ⚠️ **남의 배치를 보거나 지울 수 없다** — id 만 보고 처리하면 URL 을 바꿔
  남의 것을 건드릴 수 있다
* **빈 칸은 자리를 지킨다** — 목록을 당기면 사용자가 정한 배치가 무너진다
* ⚠️ **마스킹 실패가 화면까지 간다** — 여러 명이 보는 관제실에서 가리지 못한
  화면을 조용히 띄우면 안 된다
* **타일 하나가 실패해도 나머지는 산다**
* **열린 이벤트가 없으면 강조도 없다**
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete

from tot_dashboard.core import multiview as MV
from tot_dashboard.core import vocabulary as V
from tot_dashboard.core.db import get_session
from tot_dashboard.core.models import (VERDICT_FALSE, Camera, DetectionFeedback,
                                       Event, MultiviewLayout, User, UserPref)
from tot_dashboard.service.main import app

PFX = "TEST-MV-"


def _purge(db):
    db.execute(sa_delete(DetectionFeedback).where(
        DetectionFeedback.camera_id.like(f"{PFX}%")))
    db.execute(sa_delete(Event).where(Event.block_id.like(f"{PFX}%")))
    db.execute(sa_delete(MultiviewLayout).where(
        MultiviewLayout.name.like("시험%")))
    db.execute(sa_delete(Camera).where(Camera.id.like(f"{PFX}%")))
    # ⚠️ 사람별 설정도 지운다. 한 시험이 「멈춤」으로 두고 끝나면 다음
    # 시험이 멈춘 화면을 받아 **엉뚱한 곳에서 실패**한다.
    db.execute(sa_delete(UserPref))
    db.commit()


@pytest.fixture
def db(db_schema, seeded_users):
    s = get_session()
    _purge(s)
    # ⚠️ 어휘표가 비어 있으면 등급 순위가 전부 0 이라 「가장 급한 것」을
    # 고를 수 없다. 운영에서는 기동 훅이 심지만 시험 DB 는 직접 심는다.
    V.seed_builtin(s)
    s.add(Camera(id=f"{PFX}A", name="시험지점A", lat=35.10, lng=129.03))
    s.add(Camera(id=f"{PFX}B", name="시험지점B", lat=35.11, lng=129.04))
    s.commit()
    MV.clear_cache()
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


# --- S-05 배치 --------------------------------------------------------------


def test_multiview_page_opens(client, db):
    r = client.get("/multiview")
    assert r.status_code == 200
    assert "멀티뷰 관제" in r.text
    assert "시험지점A" in r.text


def test_page_warns_it_is_not_live_video(client, db):
    """⚠️ 정지화면을 실시간이라고 오해하면 안 된다."""
    r = client.get("/multiview")
    assert "실시간 영상이 아니라" in r.text


def test_save_layout(client, db):
    r = client.post("/multiview/save", data={
        "name": "시험배치", "tiles": "4",
        "slot_0": f"{PFX}A", "slot_1": "", "slot_2": f"{PFX}B", "slot_3": "",
        "make_default": "1"})
    assert r.status_code == 200
    db.expire_all()
    row = db.query(MultiviewLayout).filter_by(name="시험배치").one()
    assert row.tiles == 4
    # ★ 빈 칸이 자리를 지킨다 — 당기면 배치가 무너진다.
    assert row.cameras == [f"{PFX}A", "", f"{PFX}B", ""]
    assert row.is_default is True


def test_bad_tile_count_is_rejected(client, db):
    r = client.post("/multiview/save", data={"name": "시험나쁨", "tiles": "7"})
    assert r.status_code == 400
    assert "분할 수는" in r.text


def test_cannot_open_other_users_layout(client, opr_client, db, seeded_users):
    """⚠️ URL 을 바꿔 남의 배치를 들여다볼 수 없다."""
    client.post("/multiview/save", data={
        "name": "시험남의것", "tiles": "4", "slot_0": f"{PFX}A",
        "slot_1": "", "slot_2": "", "slot_3": ""})
    db.expire_all()
    mine = db.query(MultiviewLayout).filter_by(name="시험남의것").one()

    r = opr_client.get(f"/multiview?layout_id={mine.id}")
    assert r.status_code == 200
    # 남의 배치 이름이 「현재 배치」로 잡히면 안 된다.
    assert 'value="시험남의것"' not in r.text


def test_cannot_delete_other_users_layout(client, opr_client, db):
    client.post("/multiview/save", data={
        "name": "시험지킴", "tiles": "4", "slot_0": "", "slot_1": "",
        "slot_2": "", "slot_3": ""})
    db.expire_all()
    mine = db.query(MultiviewLayout).filter_by(name="시험지킴").one()

    opr_client.post("/multiview/delete", data={"layout_id": mine.id})
    db.expire_all()
    assert db.query(MultiviewLayout).filter_by(name="시험지킴").count() == 1


def test_normalize_pads_and_truncates():
    assert MV.normalize(["A"], 4) == ["A", "", "", ""]
    assert MV.normalize(["A", "B", "C", "D", "E"], 4) == ["A", "B", "C", "D"]


# --- S-05 타일·강조 ---------------------------------------------------------


def test_tile_of_unknown_camera_reports_error(client, db):
    """⚠️ 한 칸이 없다고 예외가 나면 안 된다 — 오류 문구를 준다."""
    r = client.get("/api/multiview/tile/NO-SUCH-CAM")
    assert r.status_code == 200
    d = r.json()
    assert d["image"] == "" and d["error"]


def test_alerts_empty_without_open_events(client, db):
    r = client.get("/api/multiview/alerts")
    assert r.status_code == 200
    assert all(not k.startswith(PFX) for k in r.json()["alerts"])


def test_alerts_report_open_event(client, db):
    db.add(Event(domain="flood", block_id=f"{PFX}A", place_name="시험지점A",
                 event_type="침수", level="경계", status="open"))
    db.commit()
    got = client.get("/api/multiview/alerts").json()["alerts"]
    assert got[f"{PFX}A"]["level"] == "경계"
    assert got[f"{PFX}A"]["rank"] == 3


def test_alerts_pick_highest_level(client, db):
    """같은 지점에 여러 건이면 **가장 급한 것**을 준다."""
    for lv in ("주의", "심각", "관심"):
        db.add(Event(domain="flood", block_id=f"{PFX}A", place_name="시험지점A",
                     event_type="침수", level=lv, status="open"))
    db.commit()
    got = client.get("/api/multiview/alerts").json()["alerts"]
    assert got[f"{PFX}A"]["level"] == "심각"


def test_closed_event_is_not_highlighted(client, db):
    db.add(Event(domain="flood", block_id=f"{PFX}A", place_name="시험지점A",
                 event_type="침수", level="심각", status="closed"))
    db.commit()
    got = client.get("/api/multiview/alerts").json()["alerts"]
    assert f"{PFX}A" not in got


# --- S-07 화면 --------------------------------------------------------------


def test_feedback_page_opens(client, db):
    r = client.get("/feedback")
    assert r.status_code == 200
    assert "탐지 피드백" in r.text


def test_page_says_judgement_is_not_training(client, db):
    """⚠️ 한 번 클릭이 곧 학습이라고 오해하면 안 된다."""
    r = client.get("/feedback")
    assert "곧바로 학습되지 않습니다" in r.text


def test_pending_shows_closed_event(client, db):
    ev = Event(domain="flood", block_id=f"{PFX}A", place_name="시험지점A",
               event_type="침수", level="경계", status="closed")
    db.add(ev)
    db.commit()
    r = client.get("/feedback")
    assert f"#{ev.id}" in r.text


def test_judge_records_verdict(client, db):
    ev = Event(domain="flood", block_id=f"{PFX}A", place_name="시험지점A",
               event_type="침수", level="경계", status="closed")
    db.add(ev)
    db.commit()

    r = client.post("/feedback/judge", data={
        "event_id": ev.id, "verdict": VERDICT_FALSE, "reason": "수면 반사"})
    assert r.status_code == 200
    db.expire_all()
    row = db.query(DetectionFeedback).filter_by(event_id=ev.id).one()
    assert row.verdict == VERDICT_FALSE and row.reason == "수면 반사"


def test_judge_without_reason_is_rejected(client, db):
    ev = Event(domain="flood", block_id=f"{PFX}A", place_name="시험지점A",
               event_type="침수", level="경계", status="closed")
    db.add(ev)
    db.commit()
    r = client.post("/feedback/judge", data={
        "event_id": ev.id, "verdict": VERDICT_FALSE, "reason": ""})
    assert r.status_code == 400
    assert "사유" in r.text


def test_judge_unknown_event_is_404(client, db):
    r = client.post("/feedback/judge", data={
        "event_id": 999999, "verdict": "true_positive"})
    assert r.status_code == 404


def test_missed_report(client, db):
    r = client.post("/feedback/missed", data={
        "camera_id": f"{PFX}A", "domain": "flood",
        "hazard_type_code": "flood_underpass",
        "occurred_at": "2026-08-19T03:00", "reason": "20분간 미탐"})
    assert r.status_code == 200
    db.expire_all()
    row = db.query(DetectionFeedback).filter_by(camera_id=f"{PFX}A").one()
    assert row.event_id is None
    assert row.occurred_at is not None


def test_missed_bad_time_is_rejected(client, db):
    r = client.post("/feedback/missed", data={
        "camera_id": f"{PFX}A", "occurred_at": "어제", "reason": "미탐"})
    assert r.status_code == 400
    assert "형식" in r.text


# --- 메뉴 ------------------------------------------------------------------


def test_menu_has_both_screens(client, db):
    r = client.get("/feedback")
    assert "/multiview" in r.text
    assert "/feedback" in r.text


# --- S-05 동작 켜기·끄기 ----------------------------------------------------
#
# 왜 시험으로 못박는가
#     멈춤은 **화면이 거짓말하기 가장 쉬운 상태**다. 갱신을 멈추면 마지막
#     그림이 그대로 남는데, 관제요원이 그것을 지금 상황으로 읽으면 **없는
#     안전을 본다.** 「멈췄다」는 표시가 하나라도 빠지면 그 순간부터 위험하다.


def test_동작_켜고_끄는_단추가_있다(client, db):
    r = client.get("/multiview")
    assert 'id="mv-toggle"' in r.text
    assert "멈추기" in r.text


def test_멈춤_경고띠가_화면에_있다(client, db):
    """★ 멈춘 화면을 「지금」으로 읽지 않게 하는 장치."""
    r = client.get("/multiview")
    assert 'id="mv-paused-banner"' in r.text
    assert "지금 상황이 아닙니다" in r.text


def test_칸마다_멈춤_표시가_있다(client, db):
    """띠 하나로는 부족하다 — 큰 화면에서 띠는 시야 밖으로 밀려난다."""
    r = client.get("/multiview")
    assert r.text.count('class="mv-stale"') >= 4


def test_마지막_갱신_시각_자리가_있다(client, db):
    """언제 가져온 그림인지 없으면 멈춤 표시가 있어도 판단할 수 없다."""
    r = client.get("/multiview")
    assert 'id="mv-lastrun"' in r.text


def test_기본은_동작_중이다(client, db):
    """⚠️ 관제 화면이 아무 말 없이 멎어 있으면 안 된다.

    저장된 값이 **없는** 사람도 켜짐으로 시작한다.
    """
    r = client.get("/multiview")
    assert "var mvIsRunning = true;" in r.text
    assert "동작 중" in r.text


def test_멈추면_불러오기_자체를_멈춘다(client, db):
    """그리기만 멈추면 서버 부담은 그대로다 — 타일 하나가 CCTV 원본 한 번이다."""
    r = client.get("/multiview")
    assert "clearInterval(mvTimer)" in r.text
    assert "clearInterval(mvAlertTimer)" in r.text


# --- 켜짐·꺼짐을 사람별로 저장 (user_prefs) ---------------------------------
#
# 왜 브라우저가 아니라 계정인가
#     관제요원은 자리를 옮겨 앉는다. 브라우저(localStorage)에 두면 옆자리
#     PC 로 가는 순간 설정이 사라지고, 본인은 **껐다고 생각한 것이 켜져
#     있는** 상태가 된다. 멈춤 설정에서 그것은 없는 안전을 보는 일이다.


def test_설정이_계정에_저장된다(client, db):
    r = client.post("/api/multiview/running", data={"running": "0"})
    assert r.status_code == 200
    assert r.json()["running"] is False

    # 다시 열면 꺼진 상태로 나온다 — 브라우저가 아니라 서버가 기억한다.
    page = client.get("/multiview")
    assert "var mvIsRunning = false;" in page.text
    assert "▶ 다시 시작" in page.text


def test_다시_켜면_켜진_채로_남는다(client, db):
    client.post("/api/multiview/running", data={"running": "0"})
    r = client.post("/api/multiview/running", data={"running": "1"})
    assert r.json()["running"] is True
    assert "var mvIsRunning = true;" in client.get("/multiview").text


def test_다른_사람의_설정에_영향을_주지_않는다(client, opr_client, db):
    """★ 야간 근무자가 멈춘 것이 주간 근무자 화면까지 멈추면 안 된다."""
    client.post("/api/multiview/running", data={"running": "0"})
    assert "var mvIsRunning = true;" in opr_client.get("/multiview").text


def test_저장_실패를_화면이_알린다(client, db):
    """⚠️ 조용히 넘기면 껐다고 믿고 자리를 뜬다."""
    r = client.get("/multiview")
    assert 'id="mv-savenote"' in r.text
    assert "저장되지 않았습니다" in r.text


def test_계정에_저장된다고_화면이_말한다(client, db):
    r = client.get("/multiview")
    assert "계정에 저장" in r.text


def test_이상한_값은_꺼짐으로_보지_않는다(client, db, seeded_users):
    """⚠️ 값이 깨졌을 때 관제 화면이 멎는 쪽으로 기울면 안 된다."""
    from tot_dashboard.core import user_prefs as UP
    u = db.query(User).filter(
        User.login_id == seeded_users["admin"][0]).one()
    UP.set_value(db, u.id, UP.KEY_MULTIVIEW_RUNNING, "쓰레기")
    db.commit()
    assert UP.flag(db, u.id, UP.KEY_MULTIVIEW_RUNNING) is True
    assert "var mvIsRunning = true;" in client.get("/multiview").text
