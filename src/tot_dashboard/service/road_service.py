"""UrbanGuard — 노면관리 독립 서비스 (Phase 2, 2026-08-31 분리).

API 게이트웨이 도입 계획(``C:\\Users\\전태건\\.claude\\plans\\
ticklish-discovering-pine.md``) Phase 2 산출물. ``service/main.py``
("platform-shell")에 있던 노면관리 API(``/api/road/*``·
``/api/road-analysis/*``·``/api/road-report/*``)와 상시 순회 워처
(``RoadContinuousWatcher``)·집중 감시(``road_live.py``, S-44)를 별도
프로세스로 옮겨, 노면관리를 다른 도메인과 독립적으로 기동·재기동할 수
있게 한다.

## `/road`·`/road/detect` 화면(HTML)은 여전히 platform-shell이 서빙한다

인파관리 분리(Phase 1)와 같은 이유 — 4개 도메인이 하나의 공용 셸을
쓰고 있어, 화면까지 이 서비스로 옮기면 좌측 메뉴·계정·공통 UI 전체를
복제해야 한다(이득보다 비용이 큼). 브라우저는 이미 상대경로
(``/api/road/*`` 등)로만 API를 호출하고 있어, 게이트웨이(nginx)가 그
경로만 이 서비스(포트 8035)로 보내면 화면 코드는 한 줄도 안 바꿔도 된다.

## `road/results.py`의 "최신 결과"는 이 프로세스 안에만 있다

``road/results.py``(``_results``/``_history``)는 순수 메모리 저장소다
(``history()``는 이미 ``core/road_history.py`` DB 테이블을 우선 읽지만,
"지점별 최신 1건"(``get()``/``summary()``)은 여전히 메모리뿐이다). 이
프로세스가 그 유일한 소유자가 된다 — platform-shell의 홈 화면
(``_road_summary()``)은 더 이상 이 값을 직접 못 읽으므로, 신설한
``core/road_history.py::latest_by_camera()``(DB 조회)로 대체했다
(``_crowd_summary()``가 ``CrowdObservation``을 직접 읽는 것과 동일한
원칙 — 계획서 §RiskStore/road·results 대체).

## core/common은 별도 서비스로 만들지 않는다

crowd_service.py와 동일 원칙 — 카메라·설정·인증은 네트워크 호출이
아니라 같은 파이썬 배포판을 이 프로세스도 그대로 임포트해서 쓴다.
인증도 ``AuthGuard``를 이 프로세스 안에서 독립적으로 돌려 세션 쿠키를
검증한다(같은 ``URBANGUARD_SECRET_KEY`` 공유).
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..common.config import PROJECT_ROOT
from ..core import error_hook as ug_error_hook
from ..core import roles as UG_ROLES
from ..core import service_control as ug_service
from ..core import settings as ug_settings
from ..core.auth import get_db
from ..core.guard import AuthGuard
from ..road import results as road_results
from ..road.mock_data import block_detail as road_block_detail
from ..road.report_generator import build_road_briefing
from ..road.situation_agent import RoadSituationAgent
from . import event_sync, road_live

load_dotenv(PROJECT_ROOT / ".env")

# 주기의 몇 배가 지나면 「갱신 지연」으로 볼지. platform-shell에 있던 것과
# 완전히 같은 값·이유(한 바퀴를 건너뛴 것은 흔하지만 두 바퀴를 놓쳤으면
# 실제로 관측되지 않고 있다는 뜻).
ROAD_STALE_FACTOR = 2.0


def road_cameras(request_host: str | None = None) -> list[dict]:
    """노면 탐지 대상 카메라. DB를 읽지 못하면 빈 목록.

    platform-shell(main.py)에 있던 것과 완전히 같은 로직 —
    ``core/cameras.py``·``core/restream.py``·``core/video_quality.py``
    를 그대로 재사용하는 순수 조회 함수라, 두 프로세스에 복제해도 상태가
    어긋날 일이 없다(둘 다 같은 DB를 본다).

    ⚠️ 2026-08-31 — platform-shell의 ``_restream_whep_url_for()`` 래퍼는
    가져오지 않는다(main.py 전체를 불러오면 flood/traffic
    PipelineRunner까지 이 프로세스 안에서 또 하나 뜬다 — crowd_service.py
    에서 같은 함정을 미리 피한 전례를 그대로 따른다). 실제 판단 로직이
    있는 ``core/restream.py``를 직접 부른다.
    """
    try:
        from ..core import cameras as _cams
        from ..core import restream as _restream
        from ..core import video_quality as _video_quality
        from ..core.db import get_session
        db = get_session()
        try:
            out = []
            for c in _cams.for_domain(db, UG_ROLES.Domain.ROAD.value):
                row = c.domain_row(UG_ROLES.Domain.ROAD.value)
                out.append({
                    "id": c.id, "name": c.name, "dept": c.dept,
                    "source_type": c.source_type,
                    "stream_url": (c.source_url or "") if c.source_type == "hls" else "",
                    "whep_url": (_restream.resolved_whep_url(c.id, db, request_host=request_host)
                                if c.source_type == "hls" else None),
                    "quality_grade": (_video_quality.grade_for(c.id)
                                      if c.source_type == "hls" else None),
                    "mode": "continuous" if (row and row.continuous) else "selective",
                })
            return out
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[road-service] 노면 카메라 목록 조회 실패: {str(e)[:120]}")
        return []


def _prime_settings() -> None:
    """기동 시 DB 설정을 캐시에 올린다. 실패해도 서비스는 떠야 한다
    (platform-shell의 같은 이름 함수와 동일한 이유)."""
    try:
        from ..core.db import get_session
        db = get_session()
        try:
            ug_settings.load_all(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[road-service] DB 설정 로드 실패, 기본값 사용: {str(e)[:120]}")


# 노면 손상 분석기. 모델은 최초 분석 요청 시 지연 로드되므로 기동은 항상 성공한다.
from ..road.live_analyzer import RoadDefectAnalyzer  # noqa: E402
_road_analyzer = RoadDefectAnalyzer()

_road_watcher = None

# ⚠️ 2026-08-31 — Phase 3: platform-shell(다른 프로세스)의 카메라·설정
# 변경(config_rev.bump())을 이 프로세스의 RoadContinuousWatcher가 순회
# 주기(기본 15분)만큼 기다리지 않고 즉시 받도록, PostgreSQL LISTEN을
# 백그라운드 스레드로 띄운다. 자세한 배경은 core/config_rev_bridge.py 참고.
_config_rev_listener_stop = None
_config_rev_listener_thread = None
_evidence_worker = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _road_watcher, _config_rev_listener_stop, _config_rev_listener_thread
    global _evidence_worker
    try:
        import threading

        from ..core import config_rev_bridge
        _config_rev_listener_stop = threading.Event()
        _config_rev_listener_thread = threading.Thread(
            target=config_rev_bridge.listen_loop,
            args=(_config_rev_listener_stop,),
            name="config-rev-listen",
            daemon=True,
        )
        _config_rev_listener_thread.start()
    except Exception as e:  # noqa: BLE001
        print(f"[road-service] config_rev LISTEN 시작 실패: {str(e)[:120]}")
    try:
        from .continuous import RoadContinuousWatcher
        _road_watcher = RoadContinuousWatcher(_road_analyzer)
        _road_watcher.start()
    except Exception as e:  # noqa: BLE001
        print(f"[road-service] 상시 탐지 워처 시작 실패: {str(e)[:120]}")
    # ⚠️ 2026-08-31(Phase 4 설계 검토 중 발견) — 증거 영상(S-88) 훅은
    # platform-shell(main.py)에서만 기동돼 있었다. road-service가 자기
    # 프로세스 안 core/evidence.py 링버퍼에 프레임은 이미 쌓고 있었지만
    # (continuous.py의 push/push_boxes 호출), 그걸 실제 파일로 굳히는
    # 훅+워커가 이 프로세스엔 없어 Phase 2(2026-08-31) 분리 이후 노면
    # 증거영상이 조용히 안 쌓이고 있었다 — 여기서 기동해 원상복구한다.
    try:
        from . import evidence_capture
        from ..core import events as ug_events
        ug_events.set_detection_hook(evidence_capture.on_detection)
        _evidence_worker = evidence_capture.EvidenceWorker()
        _evidence_worker.start()
    except Exception as e:  # noqa: BLE001
        print(f"[road-service] 증거 수집 시작 실패: {str(e)[:120]}")
    yield
    # 집중 감시(S-44)는 운영자가 켠 임시 스레드다. 종료 시 남겨 두면
    # 프로세스가 내려가지 않는다.
    road_live.manager.stop("서비스가 종료되었습니다.")
    if _road_watcher is not None:
        try:
            _road_watcher.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[road-service] 상시 탐지 워처 종료 실패: {str(e)[:120]}")
    if _evidence_worker is not None:
        try:
            _evidence_worker.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[road-service] 증거 수집 워커 종료 실패: {str(e)[:120]}")
    if _config_rev_listener_stop is not None:
        _config_rev_listener_stop.set()
    if _config_rev_listener_thread is not None:
        _config_rev_listener_thread.join(timeout=3.0)


_prime_settings()

app = FastAPI(title="UrbanGuard — 노면관리 서비스", lifespan=lifespan)
# platform-shell·crowd-service와 완전히 같은 미들웨어 순서.
app.add_middleware(AuthGuard)
app.add_middleware(ug_error_hook.ErrorCaptureMiddleware)


@app.get("/api/health")
def api_health():
    """이 서비스만의 헬스체크 — platform-shell의 ``/api/health``에서
    떨어져 나온 ``continuous.road``가 여기로 옮겨왔다."""
    return {
        "status": "ok",
        # 2026-09-01 — 서비스 관리(관리자 전용, /admin/services)가 쓴다.
        "pid": os.getpid(),
        "uptime_sec": round(ug_service.uptime_seconds(), 1),
        "supervised": ug_service.is_supervised(),
        "continuous": {
            "road": (_road_watcher.status() if _road_watcher is not None
                     else {"running": False, "targets": []}),
        },
        "focus": road_live.manager.status(),
    }


@app.get("/api/road/blocks")
def api_road_blocks():
    """노면 현황. **실제 분석 결과**를 싣고, 분석 전 지점은 「미분석」이다.

    모의값으로 채우지 않는 이유 — 운영자가 실제 점검 결과로 오해한다.
    """
    cams = road_cameras()
    blocks = []
    for c in cams:
        item = road_results.summary(c["id"], c["name"])
        item["mode"] = c["mode"]
        item["source_type"] = c["source_type"]
        blocks.append(item)
    return {"blocks": blocks,
            "analyzed": sum(1 for b in blocks if b["analyzed"]),
            "mock": False}


def _road_live_points(request_host: str | None = None) -> tuple[list[dict], dict, dict]:
    """실시간 관제 카드에 필요한 지점 목록과 워처 상태."""
    cont = (_road_watcher.status() if _road_watcher is not None
            else {"running": False, "targets": [], "period_sec": 0,
                  "last_round": []})
    focus = road_live.manager.status()
    period = float(cont.get("period_sec") or 0) or 900.0
    focused_id = focus.get("camera_id") if focus.get("active") else None
    watched = {t["id"] for t in (cont.get("targets") or [])}
    analyzing_id = (cont.get("current") or {}).get("id")

    points = []
    for c in road_cameras(request_host):
        item = road_results.summary(c["id"], c["name"])
        item["mode"] = c["mode"]
        item["source_type"] = c["source_type"]
        item["stream_url"] = c.get("stream_url") or ""
        item["whep_url"] = c.get("whep_url")
        item["quality_grade"] = c.get("quality_grade")
        item["dept"] = c.get("dept") or ""
        item["focused"] = (c["id"] == focused_id)
        item["section_m"] = road_results.section_length(c["id"])
        item["analyzing"] = (c["id"] == analyzing_id)
        item["pending"] = (c["mode"] == "continuous" and c["id"] not in watched
                           and bool(cont.get("running")))
        if item["focused"]:
            expected = float(focus.get("period_sec") or 60.0)
        elif c["mode"] == "continuous":
            expected = period
        else:
            expected = 0.0
        item["expected_period_sec"] = expected
        age = item.get("age_sec")
        item["stale"] = bool(expected and age is not None
                             and age > expected * ROAD_STALE_FACTOR)
        item["history"] = road_results.history(c["id"], limit=12)
        points.append(item)
    return points, cont, focus


@app.get("/api/road/live")
def api_road_live(request: Request):
    """S-44 실시간 관제. 지점별 최신 상태·경과 시간·라이브 스트림 주소."""
    from ..core import road_history
    from ..road import dataset_collector

    points, cont, focus = _road_live_points(request.url.hostname)
    analyzed = [p for p in points if p["analyzed"]]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "continuous": cont,
        "focus": focus,
        "collect": dataset_collector.status(),
        "stale_factor": ROAD_STALE_FACTOR,
        "points": points,
        "counts": {
            "total": len(points),
            "continuous": sum(1 for p in points if p["mode"] == "continuous"),
            "analyzed": len(analyzed),
            "failed": sum(1 for p in points if p["failed"]),
            "stale": sum(1 for p in points if p["stale"]),
            "pending": sum(1 for p in points if p["pending"]),
            "defects": sum(p["defect_count"] for p in analyzed),
        },
        "detection_stats": road_history.detection_stats(),
        "calibration": {
            "total": len(points),
            "with_section": sum(1 for p in points if p.get("section_m")),
        },
    }


class RoadFocusRequest(BaseModel):
    """집중 감시 시작 요청."""
    camera_id: str
    period_sec: float | None = Field(default=None, ge=30.0, le=600.0)
    ttl_min: float | None = Field(default=None, ge=1.0, le=180.0)


def _road_block(camera_id: str) -> dict | None:
    """노면 분석 대상 카메라를 DB에서 찾아 파이프라인이 쓰는 형태로 바꾼다."""
    try:
        from ..core import cameras as _cams
        from ..core.db import get_session
        db = get_session()
        try:
            cam = _cams.get(db, camera_id)
            row = cam.domain_row(UG_ROLES.Domain.ROAD.value) if cam else None
            if cam is None or row is None or not row.enabled:
                return None
            return _cams.to_block_dict(cam)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return None


@app.post("/api/road/live/focus")
def api_road_focus_start(req: RoadFocusRequest):
    """지점 하나를 짧은 주기로 반복 관측한다(한 번에 한 지점)."""
    cam = next((x for x in road_cameras() if x["id"] == req.camera_id), None)
    if cam is None:
        return {"error": "노면 탐지 대상으로 지정된 지점이 아닙니다.",
                "camera_id": req.camera_id}
    block = _road_block(req.camera_id)
    if block is None:
        return {"error": "지점 정보를 읽지 못했습니다. CCTV 관리(S-80)에서 "
                         "「도로 노면 관리」 사용 여부를 확인해 주세요.",
                "camera_id": req.camera_id}
    started = road_live.manager.start(
        _road_analyzer, cam["id"], cam["name"], block,
        period_sec=req.period_sec or road_live.FOCUS_PERIOD_SEC,
        ttl_sec=(req.ttl_min * 60.0) if req.ttl_min else road_live.FOCUS_TTL_SEC)
    return {"ok": True, "focus": {"active": True, **started},
            "note": f"「{cam['name']}」 집중 감시를 시작했습니다. "
                    f"첫 결과는 20~30초 뒤에 반영됩니다."}


@app.post("/api/road/live/focus/stop")
def api_road_focus_stop():
    """집중 감시를 중지한다."""
    last = road_live.manager.stop()
    if last is None:
        return {"ok": True, "note": "진행 중인 집중 감시가 없습니다."}
    return {"ok": True, "last": last,
            "note": f"「{last.get('camera_name') or last.get('camera_id')}」 "
                    f"집중 감시를 중지했습니다(총 {last.get('rounds', 0)}회 관측)."}


@app.get("/api/road/history/{camera_id}")
def api_road_history(camera_id: str, limit: int = 50):
    """지점 하나의 **관측 이력**. S-44 에서 지점을 고르면 읽는다."""
    cam = next((x for x in road_cameras() if x["id"] == camera_id), None)
    rows = road_results.history(camera_id, limit=max(min(limit, 200), 1))
    return {
        "camera_id": camera_id,
        "name": (cam or {}).get("name") or camera_id,
        "mode": (cam or {}).get("mode"),
        "count": len(rows),
        "history": list(reversed(rows)),
        "persisted": road_results.history_is_persistent(),
    }


class RoadCollectRequest(BaseModel):
    enabled: bool


@app.post("/api/road/collect")
def api_road_collect(req: RoadCollectRequest, request: Request,
                     db=Depends(get_db)):
    """노면 학습 데이터 자동 수집을 켜고 끈다."""
    from ..core import audit as ug_audit
    from ..core.auth import client_ip
    from ..road import dataset_collector

    before = ug_settings.get(ug_settings.KEY_ROAD_COLLECT, db)
    after = "on" if req.enabled else "off"
    ug_settings.set_value(db, ug_settings.KEY_ROAD_COLLECT, after)
    ug_audit.record(db, action=ug_audit.SETTINGS_UPDATE,
                    user=getattr(request.state, "user", None),
                    ip=client_ip(request),
                    target="노면 학습 데이터 자동 수집",
                    before={"road_collect": before},
                    after={"road_collect": after})
    db.commit()
    return {"ok": True, "collect": dataset_collector.status(),
            "note": ("학습 데이터 수집을 시작했습니다. 사람은 자동으로 가리지만 "
                     "차량번호판은 가리지 못하므로, 외부 반출 전 육안 확인이 "
                     "필요합니다." if req.enabled
                     else "학습 데이터 수집을 중지했습니다. 이미 저장된 프레임은 "
                          "그대로 남습니다.")}


@app.post("/api/road/collect/video")
async def api_road_collect_video(request: Request,
                                 file: UploadFile = File(...),
                                 label: str = Form(""),
                                 interval_sec: float = Form(2.0),
                                 max_frames: int = Form(60),
                                 db=Depends(get_db)):
    """담당자가 가진 영상에서 학습용 프레임을 뽑는다."""
    from ..core import audit as ug_audit
    from ..core import video_store as ug_video
    from ..core.auth import client_ip
    from ..road import dataset_collector

    staged = None
    try:
        staged = await ug_video.stage_upload(file, "TRAINSRC")
    except ug_video.VideoError as e:
        return {"ok": False, "error": str(e)}

    try:
        res = await run_in_threadpool(
            dataset_collector.ingest_video, staged,
            label=(label or file.filename or "").strip(),
            interval_sec=interval_sec, max_frames=max_frames)
    finally:
        ug_video.discard(staged)

    if res.get("ok"):
        try:
            ug_audit.record(db, action=ug_audit.SETTINGS_UPDATE,
                            user=getattr(request.state, "user", None),
                            ip=client_ip(request),
                            target="노면 학습 영상 프레임 추출",
                            after={"label": label or file.filename,
                                   "frames": res.get("saved"),
                                   "dir": res.get("dir_id")})
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        res["note"] = (
            f"{res['saved']}장을 추출했습니다"
            + (f" (마스킹 실패로 {res['mask_skipped']}장 제외)."
               if res.get("mask_skipped") else ".")
            + " 원본 영상은 보관하지 않습니다. 차량번호판은 가리지 못하므로 "
              "외부 반출 전 육안 확인이 필요합니다.")
    res["collect"] = dataset_collector.status()
    return res


@app.get("/api/road/{block_id}")
def api_road_block(block_id: str):
    """지점 하나의 노면 탐지 결과. 상세 패널이 읽는다."""
    cam = next((x for x in road_cameras() if x["id"] == block_id), None)
    if cam is None:
        return {"error": "block not found", "block_id": block_id}

    base = {"block_id": block_id, "name": cam["name"], "dept": cam.get("dept", ""),
            "mode": cam["mode"], "source_type": cam["source_type"], "mock": False}
    summary = road_results.summary(block_id, cam["name"])
    r = road_results.get(block_id)
    if r is None or summary["failed"]:
        return {**base, "analyzed": False, "failed": summary["failed"],
                "grade": None, "grade_label": summary["grade_label"],
                "defects": [], "defect_count": 0,
                "frames_analyzed": summary["frames_analyzed"],
                "analyzed_at": summary["analyzed_at"], "note": summary["note"]}
    return {**base, "analyzed": True, "failed": False,
            "analyzed_at": r.get("analyzed_at"), **{k: v for k, v in r.items()
                                                    if k != "analyzed_at"}}


class RoadAnalyzeRequest(BaseModel):
    """노면 손상 분석 요청. mode에 따라 target 의미가 달라진다."""
    mode: Literal["video", "cctv"]
    target: str
    conf: float | None = None
    max_frames: int | None = None
    duration_sec: float | None = Field(default=None, ge=1.0, le=60.0)


@app.get("/api/road-analysis/targets")
def api_road_analysis_targets():
    """관리자가 고를 수 있는 분석 대상 목록(기존 영상 / 실시간 CCTV)."""
    from ..road.live_analyzer import list_available_videos, list_cctv_targets
    cctv = []
    try:
        from ..core import cameras as _cams
        from ..core.db import get_session
        db = get_session()
        try:
            cctv = []
            for c in _cams.for_domain(db, UG_ROLES.Domain.ROAD.value):
                if c.source_type not in ("hls", "video"):
                    continue
                row = c.domain_row(UG_ROLES.Domain.ROAD.value)
                cctv.append({
                    "id": c.id, "name": c.name,
                    "mode": "continuous" if (row and row.continuous) else "selective",
                })
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        cctv = []
    return {
        "videos": list_available_videos(),
        "cctv": cctv or list_cctv_targets(),
        "model": _road_analyzer.model_status(),
    }


@app.post("/api/road-analysis/run")
async def api_road_analysis_run(req: RoadAnalyzeRequest):
    """선택한 모드로 노면 손상 분석을 실행한다."""
    block = None
    if req.mode == "cctv":
        block = _road_block(req.target)
    result = await run_in_threadpool(
        _road_analyzer.analyze,
        mode=req.mode, target=req.target, conf=req.conf,
        max_frames=req.max_frames, duration_sec=req.duration_sec, block=block)
    data = result.to_dict()
    if req.mode == "cctv":
        road_results.record(req.target, data, source="manual")
        event_sync.record_road_result(data)
    return data


@app.get("/api/road-report/{block_id}")
def api_road_report(block_id: str):
    """도로 노면 점검 보고서."""
    name = None
    try:
        from ..core import cameras as _cams
        from ..core.db import get_session
        db = get_session()
        try:
            cam = _cams.get(db, block_id)
            row = cam.domain_row(UG_ROLES.Domain.ROAD.value) if cam else None
            if cam is not None and row is not None and row.enabled:
                name = cam.name
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[road-service] 지점 조회 실패: {str(e)[:120]}")
    if name is None:
        return {"error": "block not found", "block_id": block_id}
    b = {"id": block_id, "name": name}
    detail = road_block_detail(block_id, b["name"])
    agent = RoadSituationAgent(use_vlm=True)
    narrative = agent.narrate_report(detail)
    md = build_road_briefing(detail, narrative=narrative)
    out_dir = PROJECT_ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"road_briefing_{block_id}_{time.strftime('%Y%m%d_%H%M%S')}.md"
    (out_dir / fname).write_text(md, encoding="utf-8")
    return {"block_id": block_id, "name": b["name"], "markdown": md,
            "filename": fname, "narrative_source": "gemini" if narrative else "rule",
            "mock": True}
