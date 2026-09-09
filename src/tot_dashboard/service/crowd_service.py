"""UrbanGuard — 인파관리 독립 서비스 (Phase 1, 2026-08-31 분리).

API 게이트웨이 도입 계획(``C:\\Users\\전태건\\.claude\\plans\\
ticklish-discovering-pine.md``) Phase 1 산출물. ``service/main.py``
("platform-shell")에 있던 인파관리 API(``/api/crowd/*``·``/api/cases*``·
``/media/{case_id}/*``)와 상시 감시 워처(``CrowdContinuousWatcher``)를
별도 프로세스로 옮겨, 인파관리를 다른 도메인과 독립적으로 기동·재기동할
수 있게 한다.

## `/crowd` 화면(HTML)은 여전히 platform-shell이 서빙한다

4개 도메인(침수·교통위험·인파관리·노면관리)이 지금 **하나의 공용 셸**
(``templates/index.html``·``static/app.js``)을 쓰고 있다 — 좌측 메뉴·
계정·공통 UI가 전부 한 번들이다. 화면까지 이 서비스로 옮기면 그 공용
셸 전체를 여기에도 복제해야 하는데(중복·유지보수 비용이 이득보다 큼),
브라우저는 이미 상대경로(``/api/crowd/*`` 등)로만 API를 호출하고 있어
게이트웨이(nginx)가 그 경로만 이 서비스(포트 8034)로 보내면 **화면
코드는 한 줄도 안 바꿔도 된다.** 그래서 이 서비스는 API·정적 미디어만
서빙하고, HTML 템플릿·정적 자원 마운트가 없다.

## core/common은 별도 서비스로 만들지 않는다

계획서 §core/common 처리 원칙 그대로 — 카메라·설정·인증은 네트워크
호출이 아니라 **같은 파이썬 배포판을 이 프로세스도 그대로 임포트**해서
쓴다. 인증도 platform-shell과 똑같이 ``AuthGuard`` 미들웨어를 이
프로세스 안에서 독립적으로 돌려 세션 쿠키를 검증한다 — 같은
``URBANGUARD_SECRET_KEY``를 공유하므로 platform-shell에서 로그인한
세션이 그대로 통한다(중앙 인증 서버 왕복이 필요 없다).

## 프로세스 하나 뜨는 값 — CaseCatalog·AlertNotifier

``notifier = AlertNotifier()``는 원래 platform-shell(main.py)에도 있었지만,
실측 확인 결과 침수·교통위험 자동 알림(``service/runner.py::
PipelineRunner``)은 **자기 자신의 독립된 ``AlertNotifier`` 인스턴스**를
따로 만들어 쓰고 있어(``self._notifier = AlertNotifier()``), main.py의
모듈 전역 ``notifier``는 사실상 인파 사례(케이스) 알림 발송에만 쓰이고
있었다. 그래서 ``notifier``와 ``/api/notifications/status``·
``/api/cases/{case_id}/notifications``를 전부 이 서비스로 옮긴다 —
platform-shell에는 더 이상 남겨 둘 이유가 없다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..common.case_archive.catalog import CaseCatalog
from ..common.config import PROJECT_ROOT
from ..common.notifier import AlertNotifier, DuplicateNotificationError, NotificationConfigurationError
from ..core import error_hook as ug_error_hook
from ..core import roles as UG_ROLES
from ..core import service_control as ug_service
from ..core import settings as ug_settings
from ..core.guard import AuthGuard
from ..crowd import cctv_analysis as crowd_cctv
from . import event_sync

load_dotenv(PROJECT_ROOT / ".env")

CROWD_DATA_ROOT = Path(
    os.environ.get("CROWD_DATA_ROOT", PROJECT_ROOT / "data" / "cases")
).resolve()


def _first_block_crowd_cfg() -> dict:
    """레거시 단일 카메라 분석기(:func:`_init_crowd_analyzer`)의 기본
    설정 — ``configs/blocks.json`` 첫 항목의 ``crowd`` 하위 설정을
    그대로 읽는다. ``service/main.py::_load_blocks_from_file()``과
    완전히 같은 파일 읽기 로직이지만, 이 서비스가 platform-shell을
    임포트하지 않도록(순환·불필요 결합 방지) 여기 그대로 복제했다.
    """
    override = os.environ.get("TOT_BLOCKS_PATH")
    path = Path(override) if override else PROJECT_ROOT / "configs" / "blocks.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            blocks = json.load(f)["blocks"]
    else:
        blocks = []
    b = blocks[0] if blocks else {}
    return dict(b), b.get("id"), (b.get("source") or {}).get("cctv_name")


def _init_crowd_analyzer():
    """인파 실시간 분석기(레거시 단일 카메라 경로, ``/api/crowd/live``
    전용). 첫 블록 설정을 쓰며, 실패해도 서비스는 계속 뜬다 — crowd
    도메인은 원래 정적 케이스 뷰어라 서비스 필수 요소가 아니다.
    """
    try:
        from ..crowd.live_analyzer import CrowdLiveAnalyzer, mock_restricted_roi
        b, block_id, node_id = _first_block_crowd_cfg()
        cfg = dict(b.get("crowd") or {})
        cfg.setdefault("intrusion_roi", mock_restricted_roi())
        cfg.setdefault("loiter_sec", 60.0)
        cfg.setdefault("commercial_zone", True)
        return CrowdLiveAnalyzer(cfg=cfg, block_id=block_id, node_id=node_id, fps=5.0)
    except Exception as e:  # noqa: BLE001
        print(f"[crowd-service] live analyzer 비활성화: {str(e)[:120]}")
        return None


_crowd_analyzer = _init_crowd_analyzer()
_crowd_t0 = time.time()
# 인파 분석기는 하나뿐이라 동시 실행을 막는다.
_crowd_lock = threading.Lock()

catalog = CaseCatalog(CROWD_DATA_ROOT)
notifier = AlertNotifier()

_crowd_watcher = None


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
        print(f"[crowd-service] DB 설정 로드 실패, 기본값 사용: {str(e)[:120]}")


_evidence_worker = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _crowd_watcher, _evidence_worker
    try:
        from .continuous import CrowdContinuousWatcher
        _crowd_watcher = CrowdContinuousWatcher()
        _crowd_watcher.start()
    except Exception as e:  # noqa: BLE001
        print(f"[crowd-service] 상시 탐지 워처 시작 실패: {str(e)[:120]}")
    # ⚠️ 2026-08-31(Phase 4 설계 검토 중 발견) — 증거 영상(S-88) 훅은
    # platform-shell(main.py)에서만 기동돼 있었다. crowd-service가 자기
    # 프로세스 안 core/evidence.py 링버퍼에 프레임은 이미 쌓고 있었지만
    # (continuous.py의 push/push_boxes 호출), 그걸 실제 파일로 굳히는
    # 훅+워커가 이 프로세스엔 없어 Phase 1(2026-08-31) 분리 이후 인파
    # 증거영상이 조용히 안 쌓이고 있었다 — 여기서 기동해 원상복구한다.
    try:
        from . import evidence_capture
        from ..core import events as ug_events
        ug_events.set_detection_hook(evidence_capture.on_detection)
        _evidence_worker = evidence_capture.EvidenceWorker()
        _evidence_worker.start()
    except Exception as e:  # noqa: BLE001
        print(f"[crowd-service] 증거 수집 시작 실패: {str(e)[:120]}")
    yield
    if _crowd_watcher is not None:
        try:
            _crowd_watcher.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[crowd-service] 상시 탐지 워처 종료 실패: {str(e)[:120]}")
    if _evidence_worker is not None:
        try:
            _evidence_worker.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[crowd-service] 증거 수집 워커 종료 실패: {str(e)[:120]}")


_prime_settings()

app = FastAPI(title="UrbanGuard — 인파관리 서비스", lifespan=lifespan)
# platform-shell(main.py)과 완전히 같은 미들웨어 순서 — 가드가 먼저(안쪽),
# 오류 수집이 나중(바깥쪽)이어야 가드 자체의 오류도 잡힌다.
app.add_middleware(AuthGuard)
app.add_middleware(ug_error_hook.ErrorCaptureMiddleware)


@app.get("/api/health")
def api_health():
    """이 서비스만의 헬스체크 — platform-shell의 ``/api/health``에서
    떨어져 나온 ``crowd_sources``·``continuous.crowd`` 필드가 여기로
    옮겨왔다. 게이트웨이 부재로 이 서비스를 직접 확인해야 할 때 쓴다.
    """
    return {
        "status": "ok",
        # 2026-09-01 — 서비스 관리(관리자 전용, /admin/services)가 쓴다.
        "pid": os.getpid(),
        "uptime_sec": round(ug_service.uptime_seconds(), 1),
        "supervised": ug_service.is_supervised(),
        "case_count": len(catalog.list_cases()),
        "crowd_sources": _crowd_source_status(),
        "continuous": {
            "crowd": (_crowd_watcher.status() if _crowd_watcher is not None
                     else {"running": False, "targets": []}),
        },
    }


def _crowd_source_status() -> dict:
    """인파 도메인이 지금 어느 모드로 도는지(임시데이터 vs 현장장비).
    platform-shell에 있던 같은 이름 함수와 동일 — 이 프로세스로 그대로
    옮겨왔다."""
    if _crowd_analyzer is None:
        return {"enabled": False}
    a = _crowd_analyzer
    st = a.source_status()
    return {
        "enabled": True,
        **st,
        "all_mock": (st["person_source"] == "mock"
                     and st["environment_mode"] == "mock"
                     and st["stereo_mode"] == "mock"),
    }


@app.get("/api/cases")
def api_list_cases():
    cases = catalog.list_cases()
    return {"cases": cases, "count": len(cases)}


@app.get("/api/cases/{case_id}")
def api_get_case(case_id: str):
    try:
        return catalog.get_case(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="분석 사례를 찾을 수 없습니다.") from exc


# ★ 등급 쏠림 (2026-08-19 전수조사, platform-shell과 동일 로직 이관).
#
#   실측 인파 관측 670회 중 627회(93.6%)가 같은 등급이었다. 등급이 늘
#   같으면 그 등급은 정보가 아니다 — 관제요원이 「지금 평소와 다른가」에
#   답할 수 없다.
#
# ⚠️ 화면이 5초마다 부르는 자리라 캐시를 둔다.
_CROWD_SPREAD_TTL_SEC = 60.0
_crowd_spread_cache: dict = {"at": 0.0, "value": None}


def _crowd_spread_cached():
    now = time.time()
    if now - _crowd_spread_cache["at"] < _CROWD_SPREAD_TTL_SEC:
        return _crowd_spread_cache["value"]
    value = None
    try:
        from ..core import crowd_history
        from ..core.db import get_session
        db = get_session()
        try:
            value = crowd_history.severity_spread(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        value = None
    _crowd_spread_cache["at"] = now
    _crowd_spread_cache["value"] = value
    return value


@app.get("/api/crowd/live")
def api_crowd_live():
    """인파 위험행동 실시간 분석 스냅샷(레거시 단일 카메라 경로).

    ⚠️ 기본값이 ``mock``이면 합성 데이터다. 응답의 ``source`` 필드로 구분할 것.
    """
    if _crowd_analyzer is None:
        return {"error": "crowd analyzer not initialised"}
    snap = _crowd_analyzer.step(time.time() - _crowd_t0)
    data = snap.to_dict()
    event_sync.record_crowd_snapshot({
        **data,
        "block_id": _crowd_analyzer.block_id,
        "node_id": _crowd_analyzer.node_id,
        "people_count": data.get("person_count"),
    })
    data["spread"] = _crowd_spread_cached()
    return data


class CrowdCctvRequest(BaseModel):
    """인파 선택 탐지 요청. camera_id 는 S-80에서 「인파 사용」으로 지정한 카메라."""
    camera_id: str
    duration_sec: float | None = None


@app.get("/api/crowd/cameras")
def api_crowd_cameras():
    """인파 탐지 대상으로 지정된 카메라 목록 (선택 탐지용)."""
    from ..core import cameras as _cams
    from ..core.db import get_session
    db = get_session()
    try:
        rows = _cams.for_domain(db, UG_ROLES.Domain.CROWD.value)
        out = []
        for c in rows:
            row = c.domain_row(UG_ROLES.Domain.CROWD.value)
            out.append({"id": c.id, "name": c.name,
                        "source_type": c.source_type,
                        "mode": "continuous" if (row and row.continuous) else "selective",
                        "has_roi": bool(c.roi_row(UG_ROLES.Domain.CROWD.value))})
        return {"cameras": out, "duration_sec": crowd_cctv.DURATION_SEC}
    finally:
        db.close()


@app.get("/api/crowd/continuous")
def api_crowd_continuous(request: Request):
    """인파 상시 카메라별 실시간 관제 — platform-shell에 있던 것과
    완전히 동일한 구현을 그대로 옮겼다(2026-08-27 신설 기능)."""
    from ..core import cameras as _cams
    from ..core import crowd_history
    from ..core import video_quality as _video_quality
    from ..core.db import get_session

    watcher_status = (_crowd_watcher.status() if _crowd_watcher is not None
                      else {"running": False, "targets": [], "interval_sec": 0})
    watched_ids = {t["id"] for t in (watcher_status.get("targets") or [])}
    interval = float(watcher_status.get("interval_sec") or 0)

    db = get_session()
    try:
        cams = _cams.for_domain(db, UG_ROLES.Domain.CROWD.value, continuous=True)
        points = []
        for c in cams:
            s = crowd_history.summary(db, c.id, c.name)
            s["dept"] = c.dept or ""
            s["stream_url"] = c.source_url if c.source_type == "hls" else ""
            # ⚠️ 2026-08-31 — platform-shell(main.py)의 ``_restream_whep_url_for()``
            # 은 임포트하지 않는다(main.py 전체를 불러오면 flood/traffic
            # PipelineRunner까지 이 프로세스 안에서 또 하나 떠 버린다 —
            # 서비스 분리 취지에 정면으로 어긋난다). 실제 판단 로직이 있는
            # ``core/restream.py``를 직접 부른다 — main.py의 래퍼도 결국
            # 이 함수 하나를 그대로 위임 호출할 뿐이다.
            from ..core import restream as _restream
            s["whep_url"] = (_restream.resolved_whep_url(c.id, db, request_host=request.url.hostname)
                             if c.source_type == "hls" else None)
            s["quality_grade"] = (_video_quality.grade_for(c.id)
                                  if c.source_type == "hls" else None)
            s["watched"] = c.id in watched_ids
            s["pending"] = c.id not in watched_ids
            s["stale"] = bool(s.get("age_sec") is not None and interval
                              and s["age_sec"] > interval * 6)
            points.append(s)
        return {
            "watcher": watcher_status,
            "points": points,
            "counts": {
                "total": len(points),
                "observed": sum(1 for p in points if p["observed"]),
                "pending": sum(1 for p in points if p["pending"]),
                "stale": sum(1 for p in points if p["stale"]),
            },
        }
    finally:
        db.close()


@app.get("/api/crowd/history/{camera_id}")
def api_crowd_history(camera_id: str, hours: int = 24, limit: int = 60):
    """인파 관측 이력과 평상시 기준선."""
    from ..core import crowd_history
    from ..core.db import get_session

    db = get_session()
    try:
        base = crowd_history.baseline(db, camera_id, hours=hours)
        rows = crowd_history.recent(db, camera_id, limit=limit)
        return {
            "camera_id": camera_id,
            "hours": hours,
            "baseline": base,
            "observations": [
                {"observed_at": r.observed_at.isoformat(),
                 "person_count": r.person_count,
                 "mean_speed": round(r.mean_speed, 1),
                 "surge": round(r.surge, 2),
                 "divergence": round(r.divergence, 2),
                 "risk_code": r.risk_code, "severity": r.severity,
                 "drivers": r.drivers, "failed": r.failed}
                for r in rows],
        }
    finally:
        db.close()


@app.post("/api/crowd/analyze")
async def api_crowd_analyze(req: CrowdCctvRequest):
    """선택한 CCTV를 관측해 인파 위험행동을 분석한다(요청 시점 1회)."""
    if _crowd_analyzer is None:
        raise HTTPException(status_code=503, detail="인파 분석기가 비활성 상태입니다.")
    from ..core import cameras as _cams
    from ..core.db import get_session
    db = get_session()
    try:
        cam = _cams.get(db, req.camera_id)
        if cam is None:
            raise HTTPException(status_code=404, detail="카메라를 찾을 수 없습니다.")
        row = cam.domain_row(UG_ROLES.Domain.CROWD.value)
        if row is None or not row.enabled:
            raise HTTPException(status_code=400,
                                detail="이 카메라는 인파 탐지 대상이 아닙니다. "
                                       "CCTV 관리에서 지정하세요.")
        if not _crowd_lock.acquire(blocking=False):
            raise HTTPException(status_code=409,
                                detail="다른 인파 분석이 진행 중입니다. 잠시 후 다시 시도하세요.")
        try:
            res = await run_in_threadpool(
                crowd_cctv.analyze, cam, _crowd_analyzer,
                duration_sec=req.duration_sec or crowd_cctv.DURATION_SEC)
        finally:
            _crowd_lock.release()
        data = res.to_dict()
        event_sync.record_crowd_snapshot({
            "block_id": cam.id, "node_id": cam.name,
            "events": res.events, "people_count": res.people_max,
            "source": "cctv",
        })
        return data
    finally:
        db.close()


class CrowdSourceRequest(BaseModel):
    """인파 소스 전환 요청. 각 필드는 생략 가능(생략 시 현재 유지)."""
    person: Literal["mock", "detector"] | None = None
    environment: Literal["mock", "device"] | None = None
    stereo: Literal["mock", "device"] | None = None


@app.post("/api/crowd/source")
def api_crowd_switch_source(req: CrowdSourceRequest):
    """인파 분석 소스를 실행 중 전환한다(임시데이터 ↔ 현장장비). 런타임
    전용 — 설정 파일에 저장하지 않는다."""
    if _crowd_analyzer is None:
        raise HTTPException(status_code=503, detail="인파 분석기가 비활성화 상태입니다.")
    return _crowd_analyzer.switch_sources(
        person=req.person, environment=req.environment, stereo=req.stereo)


@app.get("/api/notifications/status")
def api_notification_status():
    """SOLAPI 발송 설정 상태. platform-shell에 있던 것을 그대로 이관 —
    실측 확인 결과 침수·교통위험 자동 알림은 자기 자신의 독립된
    ``AlertNotifier``를 따로 쓰고 있어(``runner.py``), 이 모듈 전역
    ``notifier``는 사실상 인파 사례 알림에만 쓰이고 있었다."""
    return notifier.status()


class NotificationRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)
    channels: list[Literal["sms", "kakao"]] = Field(min_length=1, max_length=2)


@app.post("/api/cases/{case_id}/notifications")
async def api_send_notification(case_id: str, request: NotificationRequest):
    try:
        case_data = catalog.get_case(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="분석 사례를 찾을 수 없습니다.") from exc

    message = next(
        (item for item in case_data.get("sms_messages", [])
         if item.get("id") == request.message_id),
        None,
    )
    if message is None:
        raise HTTPException(status_code=404, detail="경보 메시지를 찾을 수 없습니다.")

    try:
        return await run_in_threadpool(
            notifier.send,
            event_key=f"{case_id}:{request.message_id}",
            message=message,
            channels=request.channels,
        )
    except DuplicateNotificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotificationConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"SOLAPI 발송 요청에 실패했습니다: {exc}") from exc


@app.get("/media/{case_id}/{asset_path:path}")
def api_media(case_id: str, asset_path: str):
    try:
        file_path = catalog.resolve_media(case_id, asset_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="미디어 파일을 찾을 수 없습니다.") from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="미디어 파일을 찾을 수 없습니다.")
    return FileResponse(file_path, filename=None)
