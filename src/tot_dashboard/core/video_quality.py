"""상시 화질 감시(Video Quality Monitoring) — CCTV 손상을 "우연히
발견"하지 않게 한다.

## 왜 필요한가 (2026-08-30)

전체 CCTV 점검 회차에서 SEOUL-357·203·217·713·231·369·331·363 8곳이
여전히 손상된 것을 발견했는데, 그 계기는 사용자가 화면을 보다가 우연히
SEOUL-363을 제보한 것 하나뿐이었다. 그때까지 손상을 알 수 있는 경로가
세 가지 있었는데 전부 사각지대였다:

1. ``mediamtx.log``의 decode error 누적치는 물어봐야 나오는 로그일 뿐,
   상시 경보가 아니다.
2. 기존 손상판정 휴리스틱(``flood/water_segmentation.py::
   is_likely_corrupted_frame``, 16×16 타일 밝기 표준편차 기반)은 깨끗한
   프레임도 오탐하는 것이 실측으로 확인돼 신뢰할 수 없다.
3. 이 휴리스틱은 침수 판정에만 걸려 있고 교통위험(YOLO) 경로에는 손상
   가드가 아예 없다 — 화면이 깨져도 차량·보행자 탐지는 그대로 돈다.

## 왜 이 방식인가 — 상시 30fps 탐지 루프는 건드리지 않는다

OpenCV ``VideoCapture``는 ffmpeg의 디코더 오류 신호를 애초에 노출하지
않는다(``cap.read()``는 성공/실패 불리언만 준다) — 이 신호를 상시
탐지 루프(``YoloDetectionSource``/``_update_flood``) 안에서 얻으려면
프레임 캡처 아키텍처 자체를 바꿔야 하는데, 이는 33개 카메라가 실시간
으로 도는 운영 중인 핵심 파이프라인을 건드리는 고위험 변경이다.

대신 이번 손상 조사에서 이미 실측 검증한 방법(RTSP 재배포 경로에 짧게
붙어 ffmpeg 자신의 stderr에 찍히는 실제 디코더 오류 문자열을 세는 것
— SEOUL-350/363 비교에 실제로 썼던 방법)을 훨씬 낮은 빈도로 도는 독립
스캐너로 분리한다. 상시 루프는 그대로 두고, "지금 이 카메라가 손상
상태인지"를 곁에서 재는 감시탑을 하나 더 세우는 방식이다 — 오탐 많은
픽셀 휴리스틱을 대신하는 "진짜" 신호이기도 하다(ffmpeg 디코더 자신이
보고하는 오류이지, 화면 내용을 보고 추측한 것이 아니다).

``core/ffmpeg_relay.py``(2026-08-29/30)와 같은 골격을 그대로 따른다 —
모듈 딕셔너리 + ``threading.RLock`` + 데몬 스레드 + ``status()`` +
``print(f"[video_quality] ...")`` 로깅, 카메라별 ``try/except`` 격리.
2026-08-30 ffmpeg 릴레이 사고(로깅이 안 보임, 카메라 1곳 예외가 전체를
막음)에서 배운 교훈을 처음부터 반영한다.
"""
from __future__ import annotations

import subprocess
import threading
import time

from ..common.cctv_capture import FFMPEG, USER_AGENT

# 카메라 1곳을 얼마나 오래 재는지(초). 짧을수록 자원 비용이 낮지만
# 간헐적 손상을 놓칠 확률이 올라간다 — 이번 세션의 수동 검증(3초 안팎
# 캡처)과 같은 길이로 맞췄다.
SAMPLE_SEC = 3
# 스캔 1회당 최대 대기 시간(초). 카메라 1곳이 응답 없어도(원본 장애·
# on-demand 콜드스타트) 전체 스캔 주기를 막지 못하게 한다 — 2026-08-30
# ffmpeg_relay 사고에서 배운 "감시가 한 곳에서 막히면 전체가 멈춘다"를
# 여기서도 미리 방어한다. R-03의 on_demand_start_timeout(기본 30초)보다
# 살짝 여유 있게 잡는다.
PER_CAMERA_TIMEOUT_SEC = 35.0
# 카메라 사이 최소 간격(초) — 스캔 주기 안에서 ffmpeg 프로세스가 한꺼번에
# 몰리지 않게 한다(core/restream.py의 _SAME_HOST_STAGGER_SEC와 같은 취지).
INTER_CAMERA_GAP_SEC = 1.0
# 스캔 주기 설정값을 못 읽을 때(설정 조회 자체가 실패하는 극단적 상황)의
# 최후 방어값 — settings.py의 클램프(60~3600)와 같은 하한.
_FALLBACK_INTERVAL_SEC = 900.0

# ⚠️ 2026-08-30 배포 직후 실기 검증 중 발견 — "non-existing pps/sps"·
# "decode_slice_header error"·"no frame!"은 **모든 on-demand 카메라가
# 새로 연결할 때마다 예외 없이 겪는 정상적인 시작 잡음**이다(MediaMTX가
# sourceOnDemand로 원본을 막 콜드스타트한 직후, 다음 키프레임+SPS/PPS가
# 오기 전까지 클라이언트가 기존 스트림 중간에 끼어든 것과 같은 상태).
# 실측: 이 신호들을 그대로 셌더니 스캔한 카메라 전부(BLOCK-BEXCO2·
# SEOUL-101·114·12·126·138·140 등)가 예외 없이 "손상 심각"으로 나왔다
# — 실제로는 SEOUL-140의 표본을 직접 열어 보니 이 잡음이 3초 표본 내내
# 반복(다음 키프레임이 3초 표본 안에 오지 않음)됐을 뿐, 정작 MediaMTX
# 손상의 진짜 신호("error while decoding MB")는 표본 끝부분에 딱 1번
# 나왔다. 오탐 많던 픽셀 휴리스틱을 대신하겠다고 만든 지표가 그보다도
# 더 오탐이 심해질 뻔했다 — **디코더가 프레임을 성공적으로 만들기
# 시작한 뒤에 나는 오류만** 진짜 손상 신호다.
DECODE_ERROR_SIGNATURES = (
    "error while decoding mb",
    "concealing errors",
    "corrupt decoded frame",
)

_lock = threading.RLock()
_last_scanned_at: dict[str, float] = {}   # camera_id -> monotonic 시각
_decode_error_count: dict[str, int] = {}  # camera_id -> 가장 최근 표본의 오류 수
_grade: dict[str, str] = {}               # camera_id -> "on"|"warn"|"crit"|"unknown"
_last_scan_ok: dict[str, bool] = {}       # camera_id -> 가장 최근 스캔이 연결에 성공했는가

_scan_thread: threading.Thread | None = None


def _scan_args(rtsp_url: str) -> list[str]:
    return [
        FFMPEG, "-y", "-loglevel", "error",
        "-user_agent", USER_AGENT, "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(SAMPLE_SEC),
        "-f", "null", "-",
    ]


def scan_one(rtsp_url: str, warn_threshold: int, crit_threshold: int) -> dict:
    """카메라 1곳을 짧게 재배포 경로(RTSP)로 붙어 ffmpeg 자신이 보고하는
    실제 디코더 오류 수를 센다. 원본이 아니라 **재배포 경로**를 재는
    것이 핵심이다 — 실제로 화면·AI 판정에 나가는 것과 같은 스트림을
    검사해야 MediaMTX 자체의 손상도 함께 잡는다.

    예외를 던지지 않는다 — 호출부(스캔 루프)가 카메라별로 개별 처리
    하므로 여기서 삼켜도 다른 카메라를 막지 않지만, ffmpeg_relay.py의
    교훈을 이 함수 자체에도 이중으로 적용해 둔다.
    """
    try:
        p = subprocess.run(_scan_args(rtsp_url), capture_output=True,
                           timeout=PER_CAMERA_TIMEOUT_SEC)
        stderr = p.stderr.decode("utf-8", "ignore").lower()
        count = sum(stderr.count(sig) for sig in DECODE_ERROR_SIGNATURES)
        ok = True
    except subprocess.TimeoutExpired:
        count, ok = 0, False
    except Exception:  # noqa: BLE001
        count, ok = 0, False
    if not ok:
        grade = "unknown"
    elif count > crit_threshold:
        grade = "crit"
    elif count > warn_threshold:
        grade = "warn"
    else:
        grade = "on"
    return {"decode_error_count": count, "grade": grade, "ok": ok}


def _scan_targets(db) -> list[str]:
    """스캔 대상 카메라 ID 목록. ``restream.sync_all_paths()``와 같은
    필터(활성·hls 소스·제외 목록 아님)를 쓴다 — 도메인 무관, 뷰어
    전용도 포함해야 SEOUL-357류를 잡는다."""
    from . import cameras as C
    from . import restream as _restream

    out = []
    for cam in C.list_all(db, active_only=True):
        if cam.source_type != "hls" or not cam.source_url:
            continue
        if _restream.is_excluded(cam.id, db):
            continue
        out.append(cam.id)
    return out


def _scan_tick() -> None:
    from . import restream as _restream
    from . import settings as ug_settings
    from .db import get_session

    db = get_session()
    try:
        if not ug_settings.video_quality_enabled(db):
            return
        if not ug_settings.restream_enabled(db):
            return  # 재배포 자체가 꺼져 있으면 잴 재배포 경로가 없다
        warn_th = ug_settings.video_quality_warn_threshold(db)
        crit_th = ug_settings.video_quality_crit_threshold(db)
        targets = _scan_targets(db)
        for camera_id in targets:
            try:
                rtsp = _restream.rtsp_url(camera_id, db)
                result = scan_one(rtsp, warn_th, crit_th)
                with _lock:
                    _last_scanned_at[camera_id] = time.monotonic()
                    _decode_error_count[camera_id] = result["decode_error_count"]
                    _grade[camera_id] = result["grade"]
                    _last_scan_ok[camera_id] = result["ok"]
                if result["grade"] in ("warn", "crit"):
                    print(f"[video_quality] {camera_id}: 손상 감지"
                          f"(등급={result['grade']}, 디코더 오류 "
                          f"{result['decode_error_count']}건/{SAMPLE_SEC}초 표본)")
            except Exception as e:  # noqa: BLE001
                # ⚠️ 2026-08-30 ffmpeg_relay 사고 재발 방지 — 카메라 1곳의
                # 예외가 나머지 카메라 스캔을 막으면 안 된다.
                print(f"[video_quality] {camera_id}: 스캔 처리 중 오류 — {str(e)[:160]}")
            time.sleep(INTER_CAMERA_GAP_SEC)
    finally:
        db.close()


def _scan_loop() -> None:
    from . import settings as ug_settings
    from .db import get_session

    print("[video_quality] 상시 화질 감시 스캔 스레드 시작")
    while True:
        interval = _FALLBACK_INTERVAL_SEC
        try:
            db = get_session()
            try:
                interval = ug_settings.video_quality_scan_interval_sec(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            _scan_tick()
        except Exception as e:  # noqa: BLE001
            print(f"[video_quality] 스캔 루프 오류: {str(e)[:160]}")
        time.sleep(max(interval, 60))


def start_scanner() -> None:
    """서비스 기동 시 1회 호출한다(main.py lifespan, ffmpeg_relay.
    start_watchdog()과 같은 자리). 기본 꺼짐(``video_quality.enabled``=0)
    이면 스레드는 뜨되 매 틱에서 곧바로 반환한다(``restream_enabled``와
    같은 fail-safe 패턴 — 설정 하나로 켜고 끌 수 있다)."""
    global _scan_thread
    with _lock:
        if _scan_thread is not None and _scan_thread.is_alive():
            return
        _scan_thread = threading.Thread(
            target=_scan_loop, daemon=True, name="video-quality-scanner")
        _scan_thread.start()


def status() -> dict:
    """/api/health 노출용 — 카메라별 등급·오류 수와 전체 집계."""
    with _lock:
        now = time.monotonic()
        return {
            "last_scanned_ago_sec": {cid: round(now - t)
                                     for cid, t in _last_scanned_at.items()},
            "decode_error_count": dict(_decode_error_count),
            "grade": dict(_grade),
            "on_count": sum(1 for g in _grade.values() if g == "on"),
            "warn_count": sum(1 for g in _grade.values() if g == "warn"),
            "crit_count": sum(1 for g in _grade.values() if g == "crit"),
            "unknown_count": sum(1 for g in _grade.values() if g == "unknown"),
        }


def grade_for(camera_id: str) -> str | None:
    """카메라 1곳의 최근 등급. 화면(routes_cameras.py)이 배지를 그릴 때
    쓴다. 스캔된 적 없으면 None — 화면이 "미측정"으로 정직하게 표시한다."""
    with _lock:
        return _grade.get(camera_id)
