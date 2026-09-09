# -*- coding: utf-8 -*-
"""상시 화질 감시(Video Quality Monitoring) — 2026-08-30 신설.

## 왜 이 시험이 있나

손상 카메라를 지금까지 "사용자가 화면을 보다가 우연히 제보"로만
발견해 온 것을 없애려고, 재배포 경로(RTSP)를 저빈도로 재서 ffmpeg
자신이 보고하는 실제 디코더 오류를 세는 스캐너(``core/video_quality.py``)
를 둔다. 이 파일은 (1) 등급 산정 로직 (2) 새 설정 4종 (3) 카메라 1곳의
예외가 나머지 스캔을 막지 못하는지(2026-08-30 ffmpeg_relay 사고의
교훈 — 감시가 한 곳에서 막히면 전체가 멈춘다) 를 고정한다.
"""
from __future__ import annotations

import subprocess

import pytest

from tot_dashboard.core import cameras as C
from tot_dashboard.core import restream as RS
from tot_dashboard.core import settings as S
from tot_dashboard.core import video_quality as VQ
from tot_dashboard.core.db import get_session

PFX = "TEST-VQ-"


def _clear_module_state():
    with VQ._lock:
        VQ._last_scanned_at.clear()
        VQ._decode_error_count.clear()
        VQ._grade.clear()
        VQ._last_scan_ok.clear()


def _purge_cameras():
    from sqlalchemy import delete
    from tot_dashboard.core.models import Camera, CameraDomain
    with get_session() as db:
        db.execute(delete(CameraDomain).where(CameraDomain.camera_id.like(f"{PFX}%")))
        db.execute(delete(Camera).where(Camera.id.like(f"{PFX}%")))
        db.commit()


@pytest.fixture(autouse=True)
def _clean(db_schema):
    _clear_module_state()
    _purge_cameras()
    db = get_session()
    try:
        S.set_video_quality_enabled(db, False)
        S.set_video_quality_scan_interval_sec(db, 900)
        S.set_video_quality_warn_threshold(db, 3)
        S.set_video_quality_crit_threshold(db, 15)
        S.set_restream_enabled(db, False)
        S.set_restream_excluded_ids(db, [])
        db.commit()
    finally:
        db.close()
    S.invalidate()
    yield
    _clear_module_state()
    _purge_cameras()
    S.invalidate()


def _add_camera(suffix: str) -> str:
    cid = f"{PFX}{suffix}"
    with get_session() as db:
        cam, errs = C.create(db, {
            "id": cid, "name": f"화질감시시험{suffix}",
            "lat": "35.1", "lng": "129.0", "source_type": "hls",
            "source_url": "https://example.test/a.m3u8"})
        assert not errs, errs
        db.commit()
    return cid


# --- 설정 (core/settings.py 4단계 패턴, restream_enabled 시험과 동일 형식) ---

def test_video_quality_enabled_기본값은_꺼짐():
    assert S.DEFAULTS[S.KEY_VIDEO_QUALITY_ENABLED] == "0"
    assert S.video_quality_enabled() is False


def test_set_video_quality_enabled_쓰레기값은_꺼짐으로_눕힌다(db_schema):
    db = get_session()
    try:
        v = S.set_video_quality_enabled(db, "아무거나")
        db.commit()
        assert v is False
        assert S.video_quality_enabled(db) is False
    finally:
        db.close()


def test_set_video_quality_enabled_참값들을_전부_켜짐으로_인식한다(db_schema):
    db = get_session()
    try:
        for truthy in (True, "1", "true", "True", 1):
            assert S.set_video_quality_enabled(db, truthy) is True
        db.commit()
        assert S.video_quality_enabled(db) is True
    finally:
        db.close()


def test_scan_interval_기본값과_클램프(db_schema):
    assert S.video_quality_scan_interval_sec() == 900
    db = get_session()
    try:
        assert S.set_video_quality_scan_interval_sec(db, 10) == 60, "하한 60초 밑으로 못 내려간다"
        assert S.set_video_quality_scan_interval_sec(db, 999999) == 3600, "상한 1시간을 넘지 않는다"
        assert S.set_video_quality_scan_interval_sec(db, "아무거나") == 900, "숫자가 아니면 기본값"
        db.commit()
    finally:
        db.close()


def test_경고_심각_임계치_기본값과_저장(db_schema):
    assert S.video_quality_warn_threshold() == 3
    assert S.video_quality_crit_threshold() == 15
    db = get_session()
    try:
        assert S.set_video_quality_warn_threshold(db, 5) == 5
        assert S.set_video_quality_crit_threshold(db, 20) == 20
        db.commit()
        assert S.video_quality_warn_threshold(db) == 5
        assert S.video_quality_crit_threshold(db) == 20
    finally:
        db.close()


# --- 등급 산정 (scan_one) ---------------------------------------------------

class _FakeCompleted:
    def __init__(self, stderr: bytes):
        self.stderr = stderr
        self.returncode = 0


def test_오류_문자열이_없으면_정상_등급(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _FakeCompleted(b"[info] stream opened cleanly"))
    result = VQ.scan_one("rtsp://127.0.0.1:8554/x", warn_threshold=3, crit_threshold=15)
    assert result == {"decode_error_count": 0, "grade": "on", "ok": True}


def test_시작_잡음_문자열은_오류로_세지_않는다(monkeypatch):
    """⚠️ 2026-08-30 배포 직후 실기 검증 중 발견 — on-demand 카메라가
    새로 연결할 때마다 예외 없이 겪는 "다음 키프레임 대기" 잡음
    (non-existing PPS/SPS·decode_slice_header error·no frame!)을 그대로
    셌더니 스캔한 카메라 전부가 예외 없이 "손상 심각"으로 나왔다 —
    이 신호들은 진짜 손상(MediaMTX HLS 디먹서 버그)이 아니라는 회귀
    시험."""
    stderr = b"\n".join([
        b"[h264 @ 0x1] non-existing PPS 0 referenced",
        b"[h264 @ 0x1] decode_slice_header error",
        b"[h264 @ 0x1] no frame!",
    ] * 20)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(stderr))
    result = VQ.scan_one("rtsp://127.0.0.1:8554/x", warn_threshold=3, crit_threshold=15)
    assert result["decode_error_count"] == 0
    assert result["grade"] == "on"


def test_오류_문자열이_경고_임계치_넘으면_주의_등급(monkeypatch):
    # 디코더가 프레임을 성공적으로 만들기 시작한 뒤에 나는 진짜 손상
    # 신호만 센다(위 시험이 고정한 원칙).
    stderr = b"\n".join([b"concealing errors"] * 4)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(stderr))
    result = VQ.scan_one("rtsp://127.0.0.1:8554/x", warn_threshold=3, crit_threshold=15)
    assert result["decode_error_count"] == 4
    assert result["grade"] == "warn"


def test_오류_문자열이_심각_임계치_넘으면_손상_심각_등급(monkeypatch):
    stderr = b"\n".join([b"error while decoding MB 24 32, bytestream -19"] * 20)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(stderr))
    result = VQ.scan_one("rtsp://127.0.0.1:8554/x", warn_threshold=3, crit_threshold=15)
    assert result["decode_error_count"] == 20
    assert result["grade"] == "crit"


def test_타임아웃이면_미측정(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=35)
    monkeypatch.setattr(subprocess, "run", _raise)
    result = VQ.scan_one("rtsp://127.0.0.1:8554/x", warn_threshold=3, crit_threshold=15)
    assert result == {"decode_error_count": 0, "grade": "unknown", "ok": False}


def test_예외가_나도_던지지_않는다(monkeypatch):
    def _raise(*a, **k):
        raise OSError("ffmpeg 실행 파일을 찾을 수 없음")
    monkeypatch.setattr(subprocess, "run", _raise)
    result = VQ.scan_one("rtsp://127.0.0.1:8554/x", warn_threshold=3, crit_threshold=15)
    assert result["grade"] == "unknown"


# --- 대상 목록 (_scan_targets) — sync_all_paths()와 같은 필터 ----------------

def test_스캔_대상은_hls_소스이면서_제외되지_않은_활성_카메라다(db_schema):
    cid_ok = _add_camera("A")
    cid_excluded = _add_camera("B")
    db = get_session()
    try:
        S.set_restream_excluded_ids(db, [cid_excluded])
        db.commit()
        targets = VQ._scan_targets(db)
    finally:
        db.close()
    assert cid_ok in targets
    assert cid_excluded not in targets


# --- status()/grade_for() — 실제 상태 반영 -----------------------------------

def test_status와_grade_for는_모듈_상태를_그대로_반영한다():
    with VQ._lock:
        VQ._grade["SEOUL-350"] = "on"
        VQ._grade["SEOUL-363"] = "crit"
        VQ._decode_error_count["SEOUL-363"] = 42
        VQ._last_scanned_at["SEOUL-363"] = __import__("time").monotonic()
    s = VQ.status()
    assert s["grade"]["SEOUL-350"] == "on"
    assert s["grade"]["SEOUL-363"] == "crit"
    assert s["decode_error_count"]["SEOUL-363"] == 42
    assert s["on_count"] == 1
    assert s["crit_count"] == 1
    assert VQ.grade_for("SEOUL-363") == "crit"
    assert VQ.grade_for("SEOUL-999-없음") is None


# --- 2026-08-30 ffmpeg_relay 사고 교훈 회귀 — 카메라 1곳의 예외가 -----------
# --- 나머지 스캔을 막으면 안 된다 -------------------------------------------

def test_한_카메라의_스캔_예외가_다른_카메라_스캔을_막지_못한다(db_schema, monkeypatch):
    cid_a = _add_camera("BOOM")
    cid_b = _add_camera("OK")
    db = get_session()
    try:
        S.set_video_quality_enabled(db, True)
        S.set_restream_enabled(db, True)
        db.commit()
    finally:
        db.close()

    def _fake_rtsp_url(camera_id, db=None):
        return f"rtsp://127.0.0.1:8554/{camera_id}"

    def _fake_scan_one(rtsp_url, warn_threshold, crit_threshold):
        if "BOOM" in rtsp_url:
            raise RuntimeError("의도적 오류(시험)")
        return {"decode_error_count": 0, "grade": "on", "ok": True}

    monkeypatch.setattr(RS, "rtsp_url", _fake_rtsp_url)
    monkeypatch.setattr(VQ, "scan_one", _fake_scan_one)
    monkeypatch.setattr(VQ.time, "sleep", lambda *_: None)  # 시험 속도

    VQ._scan_tick()

    assert VQ.grade_for(cid_b) == "on", "A의 예외 때문에 B까지 스캔이 안 됐다"
    assert VQ.grade_for(cid_a) is None, "예외가 난 카메라는 등급이 기록되지 않아야 한다"
