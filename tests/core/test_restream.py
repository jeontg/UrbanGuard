# -*- coding: utf-8 -*-
"""CCTV 재배포 허브(MediaMTX) 연동 — 2026-08-28 신설.

## 왜 이 시험이 있나

4개 탐지 서비스가 원본 CCTV에 개별 연결해 세 번째 연결이 거절되는 문제
(`road/live_analyzer.py` 주석 실측)를 MediaMTX 재배포로 해결한다.
``core/restream.py``는 URL 조립과 Control API 연동만 담당하며, **재배포는
부가 기능이지 카메라 CRUD의 필수 조건이 아니다** — Control API 호출이
실패해도 예외가 밖으로 나가면 안 된다. 이 시험이 그 계약을 고정한다.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from tot_dashboard.core import restream as RS
from tot_dashboard.core import settings as S
from tot_dashboard.core.db import get_session


@pytest.fixture(autouse=True)
def _clean(db_schema):
    RS.clear_health_cache()
    db = get_session()
    try:
        S.set_restream_enabled(db, False)
        S.set_restream_excluded_ids(db, [])
        S.set_restream_on_demand(db, False)
        S.set_restream_relay_ids(db, [])
        # ⚠️ 2026-08-29 실측 발견 — public_host를 리셋 안 하면
        # `test_whep_url_public_host_설정이_있으면_우선한다`가 남긴
        # "192.168.0.10"이 DB에 그대로 남아, 이후 이 파일의 다른
        # 시험(그리고 캐시가 비어 db로 재조회하는 어떤 시험이든)이
        # public_host를 안 건드렸는데도 그 값을 물려받는다 — 값이
        # 세션 전체(db_schema는 session-scope)에 걸쳐 DB row로
        # 영구 저장되기 때문이다. 매 시험 시작 전 전부 기본값으로
        # 되돌린다.
        S.set_restream_hosts_ports(db, host="127.0.0.1", public_host="",
                                   rtsp_port="8554", whep_port="8889",
                                   api_port="9997")
        db.commit()
    finally:
        db.close()
    S.invalidate()
    yield
    RS.clear_health_cache()
    S.invalidate()


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- 설정 (core/settings.py 4단계 패턴) -------------------------------------

def test_restream_enabled_기본값은_꺼짐():
    """새 단일 장애점(SPOF)이므로 검증 전에는 안전측(꺼짐)이어야 한다."""
    assert S.DEFAULTS[S.KEY_RESTREAM_ENABLED] == "0"
    assert S.restream_enabled() is False


def test_set_restream_enabled_쓰레기값은_꺼짐으로_눕힌다(db_schema):
    db = get_session()
    try:
        v = S.set_restream_enabled(db, "아무거나")
        db.commit()
        assert v is False
        assert S.restream_enabled(db) is False
    finally:
        db.close()


def test_set_restream_enabled_참값들을_전부_켜짐으로_인식한다(db_schema):
    db = get_session()
    try:
        for truthy in (True, "1", "true", "True", 1):
            v = S.set_restream_enabled(db, truthy)
            assert v is True
        db.commit()
        assert S.restream_enabled(db) is True
    finally:
        db.close()


def test_기본_호스트_포트값(db_schema):
    assert S.restream_host() == "127.0.0.1"
    assert S.restream_rtsp_port() == "8554"
    assert S.restream_whep_port() == "8889"
    assert S.restream_api_port() == "9997"


# --- URL 조립 ----------------------------------------------------------------

def test_rtsp_url_형식(db_schema):
    assert RS.rtsp_url("SEOUL-1042") == "rtsp://127.0.0.1:8554/SEOUL-1042"


def test_whep_url_기본은_restream_host를_쓴다(db_schema):
    assert RS.whep_url("SEOUL-1042") == "http://127.0.0.1:8889/SEOUL-1042/whep"


def test_whep_url_public_host_설정이_있으면_우선한다(db_schema):
    db = get_session()
    try:
        S.set_restream_hosts_ports(db, public_host="192.168.0.10")
        db.commit()
        assert RS.whep_url("SEOUL-1042", db) == "http://192.168.0.10:8889/SEOUL-1042/whep"
    finally:
        db.close()


def test_whep_url_public_host가_없으면_request_host를_쓴다(db_schema):
    assert (RS.whep_url("SEOUL-1042", request_host="ug.example.internal")
            == "http://ug.example.internal:8889/SEOUL-1042/whep")


# --- resolved_whep_url() — "재배포 가능한가" 판단 통합 (2026-08-29, R-01) ---
#
# runner.py::_restream_whep_url()·main.py::_restream_whep_url_for()가
# 각각 따로 구현하던 enabled→제외목록→헬스체크→URL조립 판단을 여기
# 하나로 합쳤다 — 같은 버그(제외 목록 미확인)가 두 곳에서 따로
# 발견·수정된 전례가 있어서다. 이 함수 하나만 옳으면 두 호출부는
# 자동으로 옳다.

def test_resolved_whep_url_재배포_꺼져있으면_None(db_schema):
    assert RS.resolved_whep_url("SEOUL-1042") is None


def test_resolved_whep_url_제외_목록이면_None(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        S.set_restream_excluded_ids(db, ["SEOUL-1042"])
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    assert RS.resolved_whep_url("SEOUL-1042", db) is None


def test_resolved_whep_url_헬스체크_실패면_None(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: False)
    assert RS.resolved_whep_url("SEOUL-1042", db) is None


def test_resolved_whep_url_정상이면_whep_url과_동일하고_request_host를_반영한다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(RS, "mediamtx_healthy", lambda *a, **k: True)
    got = RS.resolved_whep_url("SEOUL-1042", db, request_host="ug.example.internal")
    assert got == "http://ug.example.internal:8889/SEOUL-1042/whep"


# --- Control API — 실패를 삼킨다 ---------------------------------------------

def test_add_path_재배포_꺼져있으면_HTTP를_아예_안_부른다(db_schema, monkeypatch):
    calls = []
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or _FakeResponse())
    assert RS.add_path("SEOUL-1042", "https://example.test/a.m3u8") is False
    assert calls == []


def test_add_path_연결_오류를_삼킨다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    def _raise(*a, **k):
        raise ConnectionRefusedError("no server")

    monkeypatch.setattr(RS.urllib.request, "urlopen", _raise)
    # 예외가 밖으로 나가면 안 된다 — 카메라 CRUD를 절대 막지 않는다.
    assert RS.add_path("SEOUL-1042", "https://example.test/a.m3u8") is False


def test_add_path_400이면_patch로_재시도한다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    calls = []

    def _urlopen(req, timeout=None):
        calls.append(req.get_method())
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, None)
        return _FakeResponse(200)

    monkeypatch.setattr(RS.urllib.request, "urlopen", _urlopen)
    ok = RS.add_path("SEOUL-1042", "https://example.test/a.m3u8")
    assert ok is True
    assert calls == ["POST", "PATCH"]


def test_remove_path_연결_오류를_삼킨다(db_schema, monkeypatch):
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert RS.remove_path("SEOUL-1042") is False


# --- 헬스체크 캐시 ------------------------------------------------------------

def test_mediamtx_healthy_TTL_안에서는_다시_안_부른다(db_schema, monkeypatch):
    calls = []
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or _FakeResponse(200))
    t = [1000.0]
    monkeypatch.setattr(RS.time, "monotonic", lambda: t[0])

    assert RS.mediamtx_healthy() is True
    t[0] += 1.0  # TTL(10초) 안
    assert RS.mediamtx_healthy() is True
    assert len(calls) == 1, "TTL 안인데 다시 HTTP를 불렀다"

    t[0] += 20.0  # TTL 밖
    assert RS.mediamtx_healthy() is True
    assert len(calls) == 2


def test_mediamtx_healthy_응답_없으면_False(db_schema, monkeypatch):
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert RS.mediamtx_healthy() is False


# --- 카메라별 재배포 제외 목록 (2026-08-28, 실기 검증 중 발견) --------------
#
# 부산시 ITS 원본 2곳(Wowza 계열)이 재생목록 세션을 1회용으로만 허용해
# MediaMTX의 표준 HLS 폴링과 근본적으로 안 맞는 것을 실기 검증(디버그
# 로그로 3분 이상 단독 관찰, 0% 성공)으로 확인했다. 이 목록에 오른
# 카메라는 재배포가 켜져 있어도 원본 직결을 유지해야 한다.

def test_excluded_ids_기본값은_빈_목록이다():
    assert S.DEFAULTS[S.KEY_RESTREAM_EXCLUDED_IDS] == ""
    assert S.restream_excluded_ids() == set()


def test_set_excluded_ids_형식이_아닌_값은_버린다(db_schema):
    db = get_session()
    try:
        kept = S.set_restream_excluded_ids(
            db, ["BLOCK-CENTUMSTN", "1등급짜리", "ab", "OK-2"])
        db.commit()
        # "1등급짜리"(비ASCII·숫자 시작), "ab"(소문자·너무 짧음)는 버려진다.
        assert kept == {"BLOCK-CENTUMSTN", "OK-2"}
        assert S.restream_excluded_ids(db) == {"BLOCK-CENTUMSTN", "OK-2"}
    finally:
        db.close()


def test_set_excluded_ids_대소문자를_정규화한다(db_schema):
    db = get_session()
    try:
        kept = S.set_restream_excluded_ids(db, ["block-olympic"])
        db.commit()
        assert kept == {"BLOCK-OLYMPIC"}
    finally:
        db.close()


def test_is_excluded는_목록에_있는_카메라만_True(db_schema):
    db = get_session()
    try:
        S.set_restream_excluded_ids(db, ["BLOCK-CENTUMSTN", "BLOCK-OLYMPIC"])
        db.commit()
    finally:
        db.close()
    assert RS.is_excluded("BLOCK-CENTUMSTN") is True
    assert RS.is_excluded("block-centumstn") is True, "대소문자 무관해야 한다"
    assert RS.is_excluded("SEOUL-1042") is False


# --- on-demand 재배포 (2026-08-29, R-03) -------------------------------
#
# 등록 33개 중 실사용 9개뿐인데 전부 24시간 원본 연결을 유지해 하루
# 239GB가 유입됐다(실측). 등록 범위는 그대로 두고 add_path()가
# MediaMTX에 sourceOnDemand를 심어, 리더가 없으면 원본 연결 자체를
# 끊는다.

def test_restream_on_demand_기본값은_꺼짐():
    assert S.DEFAULTS[S.KEY_RESTREAM_ON_DEMAND] == "0"
    assert S.restream_on_demand() is False


def test_set_restream_on_demand_쓰레기값은_꺼짐으로_눕힌다(db_schema):
    db = get_session()
    try:
        v = S.set_restream_on_demand(db, "아무거나")
        db.commit()
        assert v is False
        assert S.restream_on_demand(db) is False
    finally:
        db.close()


def test_set_restream_on_demand_참값들을_전부_켜짐으로_인식한다(db_schema):
    db = get_session()
    try:
        for truthy in (True, "1", "true", "True", 1):
            v = S.set_restream_on_demand(db, truthy)
            assert v is True
        db.commit()
        assert S.restream_on_demand(db) is True
    finally:
        db.close()


def test_on_demand_타임아웃_기본값(db_schema):
    assert S.restream_on_demand_start_timeout_sec() == "30"
    assert S.restream_on_demand_close_after_sec() == "60"


def test_add_path_on_demand_꺼져있으면_기존처럼_source만_보낸다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    sent = []

    def _urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse(200)

    monkeypatch.setattr(RS.urllib.request, "urlopen", _urlopen)
    ok = RS.add_path("SEOUL-1042", "https://example.test/a.m3u8")
    assert ok is True
    assert sent == [{"source": "https://example.test/a.m3u8"}], (
        "on_demand가 꺼져 있는데 sourceOnDemand 필드가 섞여 들어갔다")


def test_add_path_on_demand_켜져있으면_바디에_필드를_싣는다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        S.set_restream_on_demand(db, True)
        db.commit()
    finally:
        db.close()

    sent = []

    def _urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse(200)

    monkeypatch.setattr(RS.urllib.request, "urlopen", _urlopen)
    ok = RS.add_path("SEOUL-1042", "https://example.test/a.m3u8")
    assert ok is True
    assert sent == [{
        "source": "https://example.test/a.m3u8",
        "sourceOnDemand": True,
        "sourceOnDemandStartTimeout": "30s",
        "sourceOnDemandCloseAfter": "60s",
    }]


def test_add_path_제외된_카메라는_HTTP를_안_부른다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        S.set_restream_excluded_ids(db, ["BLOCK-CENTUMSTN"])
        db.commit()
    finally:
        db.close()
    calls = []
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or _FakeResponse())
    assert RS.add_path("BLOCK-CENTUMSTN", "https://example.test/a.m3u8") is False
    assert calls == [], "제외된 카메라인데 MediaMTX를 불렀다"


def test_sync_all_paths_제외된_카메라는_excluded로_분류된다(db_schema, monkeypatch):
    from sqlalchemy import delete as sa_delete

    from tot_dashboard.core import cameras as C
    from tot_dashboard.core.models import Camera

    pfx = "TEST-RS-EXCL-"
    db = get_session()
    try:
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        for suffix, url in (("A", "https://example.test/a.m3u8"),
                            ("B", "https://example.test/b.m3u8")):
            _, errs = C.create(db, {
                "id": f"{pfx}{suffix}", "name": f"제외시험{suffix}",
                "lat": "35.1", "lng": "129.0", "source_type": "hls",
                "source_url": url})
            assert not errs, errs
        db.commit()

        S.set_restream_enabled(db, True)
        S.set_restream_excluded_ids(db, [f"{pfx}A"])
        db.commit()

        monkeypatch.setattr(RS.urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(200))
        result = RS.sync_all_paths(db)
        assert f"{pfx}A" in result["excluded"]
        assert f"{pfx}A" not in result["failed"]
        assert result["ok"] >= 1  # 적어도 B는 시도된다
    finally:
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        db.commit()
        db.close()


def test_sync_all_paths_같은_호스트는_순차_등록_사이에_간격을_둔다(db_schema, monkeypatch):
    """★ 2026-08-28 실기 검증 중 발견 — 같은 원본 서버(호스트) 카메라를
    한꺼번에 등록했더니 여러 곳이 응답 없이 멈췄고, 8초 간격으로 순차
    등록하니 전부 정상 연결됐다(strm4.spatic.go.kr 14곳 실측). 같은
    호스트로 가는 등록 사이에만 최소 간격(``_SAME_HOST_STAGGER_SEC``)을
    두는지, 서로 다른 호스트는 간격 없이 바로 등록하는지 고정한다."""
    from sqlalchemy import delete as sa_delete

    from tot_dashboard.core import cameras as C
    from tot_dashboard.core.models import Camera

    pfx = "TEST-RS-STAGGER-"
    db = get_session()
    try:
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        for suffix, url in (
            ("A1", "https://host-a.test/a1.m3u8"),
            ("A2", "https://host-a.test/a2.m3u8"),  # A1과 같은 호스트
            ("B1", "https://host-b.test/b1.m3u8"),  # 다른 호스트
        ):
            _, errs = C.create(db, {
                "id": f"{pfx}{suffix}", "name": f"간격시험{suffix}",
                "lat": "35.1", "lng": "129.0", "source_type": "hls",
                "source_url": url})
            assert not errs, errs
        db.commit()
        S.set_restream_enabled(db, True)
        db.commit()

        monkeypatch.setattr(RS.urllib.request, "urlopen",
                            lambda *a, **k: _FakeResponse(200))
        t = [1000.0]
        monkeypatch.setattr(RS.time, "monotonic", lambda: t[0])
        sleeps = []

        def _fake_sleep(sec):
            sleeps.append(sec)
            t[0] += sec  # 실제로 시간이 흐른 것처럼 시계도 같이 전진시킨다

        monkeypatch.setattr(RS.time, "sleep", _fake_sleep)

        result = RS.sync_all_paths(db)
        assert result["ok"] == 3
        # 카메라 목록 정렬 순서(ID 오름차순)상 A1 → A2 → B1로 등록된다.
        # A1→A2(같은 호스트)에서만 간격을 둔다 — 정확히 1번.
        assert sleeps == [RS._SAME_HOST_STAGGER_SEC]
    finally:
        db.execute(sa_delete(Camera).where(Camera.id.like(f"{pfx}%")))
        db.commit()
        db.close()


# --- ffmpeg 릴레이 (2026-08-29, 같은 날 후속) --------------------------
#
# MediaMTX 자신의 HLS 디먹서가 일부 카메라 영상을 간헐적으로 손상시키는
# 것을 실측으로 확인했다(원본 직결은 깨끗한데 MediaMTX 재배포만 손상).
# 화이트리스트(restream.relay_ids)에 오른 카메라는 MediaMTX가 원본을
# 직접 안 읽고, ffmpeg가 대신 읽어 RTSP로 발행한다.

def test_relay_ids_기본값은_빈_목록이다():
    assert S.DEFAULTS[S.KEY_RESTREAM_RELAY_IDS] == ""
    assert S.restream_relay_ids() == set()


def test_is_relay_managed는_화이트리스트에_있는_카메라만_True(db_schema):
    db = get_session()
    try:
        S.set_restream_relay_ids(db, ["SEOUL-1042"])
        db.commit()
    finally:
        db.close()
    assert RS.is_relay_managed("SEOUL-1042") is True
    assert RS.is_relay_managed("seoul-1042") is True, "대소문자 무관해야 한다"
    assert RS.is_relay_managed("SEOUL-19") is False


def test_add_path는_릴레이_대상이면_pull_방식을_안_타고_릴레이를_띄운다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        S.set_restream_relay_ids(db, ["SEOUL-1042"])
        db.commit()
    finally:
        db.close()

    sent = []
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda req, timeout=None: sent.append(
                            (req.full_url, req.data)) or _FakeResponse(200))
    relay_calls = []
    import tot_dashboard.core.ffmpeg_relay as relay_mod
    monkeypatch.setattr(relay_mod, "start",
                        lambda cid, src, pub: relay_calls.append((cid, src, pub)) or True)

    ok = RS.add_path("SEOUL-1042", "https://example.test/a.m3u8", db)
    assert ok is True
    assert relay_calls == [("SEOUL-1042", "https://example.test/a.m3u8",
                            "rtsp://127.0.0.1:8554/SEOUL-1042")]
    # MediaMTX에는 "발행자를 기다리는" 경로만 등록됐어야 한다 — 원본
    # URL이 그대로 source:로 들어가는 풀 방식 바디가 아니어야 한다.
    assert len(sent) == 1
    import json
    body = json.loads(sent[0][1].decode("utf-8"))
    assert body == {"source": "publisher"}


def test_add_path는_릴레이_대상이_아니면_기존처럼_풀_방식을_쓴다(db_schema, monkeypatch):
    db = get_session()
    try:
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    sent = []
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda req, timeout=None: sent.append(req.data) or _FakeResponse(200))
    relay_calls = []
    import tot_dashboard.core.ffmpeg_relay as relay_mod
    monkeypatch.setattr(relay_mod, "start", lambda *a, **k: relay_calls.append(1) or True)

    ok = RS.add_path("SEOUL-19", "https://example.test/a.m3u8", db)
    assert ok is True
    assert relay_calls == [], "릴레이 대상이 아닌데 ffmpeg_relay.start()가 불렸다"
    import json
    assert json.loads(sent[0].decode("utf-8")) == {"source": "https://example.test/a.m3u8"}


def test_remove_path는_릴레이_대상이_아니어도_항상_stop을_부른다(db_schema, monkeypatch):
    """no-op 안전성 회귀 — remove_path()는 이 카메라가 릴레이 대상인지
    몰라도(또는 확인 없이) 항상 ffmpeg_relay.stop()을 불러야 한다."""
    monkeypatch.setattr(RS.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse(200))
    stop_calls = []
    import tot_dashboard.core.ffmpeg_relay as relay_mod
    monkeypatch.setattr(relay_mod, "stop", lambda cid: stop_calls.append(cid))

    RS.remove_path("SEOUL-19")
    assert stop_calls == ["SEOUL-19"]
