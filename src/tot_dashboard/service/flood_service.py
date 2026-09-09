"""UrbanGuard — 침수 독립 서비스 (Phase 4, 2026-08-31 분리).

API 게이트웨이 도입 계획(``C:\\Users\\전태건\\.claude\\plans\\
ticklish-discovering-pine.md``) Phase 4 산출물. `service/runner.py::
PipelineRunner`에서 침수·하천 부분만 잘라낸 `service/runner_flood.py::
FloodPipelineRunner`를 이 별도 프로세스(포트 8036)에서 돌린다.

`/flood` 화면(HTML)은 여전히 platform-shell이 서빙한다 — crowd/road와
같은 이유(4개 도메인 공용 셸, §Phase 4 상세 설계 참고). `/api/blocks`도
DB 직접 조회라 runner에 의존하지 않아 platform-shell에 그대로 둔다.

이 서비스로 옮기는 것은 **runner(FloodPipelineRunner)가 실제로 있어야만
답할 수 있는 API**뿐이다 — `/api/flood-risk*`·`/api/stream/flood-risk`.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ..common.config import PROJECT_ROOT
from ..core import error_hook as ug_error_hook
from ..core import service_control as ug_service
from ..core import settings as ug_settings
from ..core.guard import AuthGuard
from . import sse
from .store import RiskStore

load_dotenv(PROJECT_ROOT / ".env")


def _load_blocks() -> list[dict]:
    """`service/main.py::_load_blocks()`와 같은 패턴이되, 침수 도메인만
    본다(교통위험은 `traffic_service.py`가 따로 로드한다)."""
    if os.environ.get("TOT_BLOCKS_PATH"):
        override = os.environ["TOT_BLOCKS_PATH"]
        path = Path(override)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)["blocks"]
        return []
    try:
        from ..core import cameras as _cams
        from ..core.db import get_session
        from ..core.roles import Domain as _Dom
        db = get_session()
        try:
            blocks = _cams.continuous_blocks(db, _Dom.FLOOD.value)
            if blocks:
                print(f"[flood-service] DB에서 상시 침수 카메라 {len(blocks)}개 로드")
            else:
                print("[flood-service] 상시 침수로 지정된 카메라가 없습니다 "
                      "— S-80에서 지정하세요")
            return blocks
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[flood-service] DB 조회 실패, 빈 목록으로 시작: {str(e)[:120]}")
        return []


def _prime_settings() -> None:
    try:
        from ..core.db import get_session
        db = get_session()
        try:
            ug_settings.load_all(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[flood-service] DB 설정 로드 실패, 기본값 사용: {str(e)[:120]}")


store = RiskStore()

from .runner_flood import FloodPipelineRunner  # noqa: E402

runner = FloodPipelineRunner(
    store, _load_blocks(),
    fps=float(os.environ.get("TOT_FPS", "5")),
    dynamic=not bool(os.environ.get("TOT_BLOCKS_PATH")))

_event_sync = None
_evidence_worker = None
_config_rev_listener_stop = None
_config_rev_listener_thread = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _event_sync, _evidence_worker, _config_rev_listener_stop, _config_rev_listener_thread
    runner.start()
    try:
        from . import evidence_capture
        from ..core import events as ug_events
        ug_events.set_detection_hook(evidence_capture.on_detection)
        _evidence_worker = evidence_capture.EvidenceWorker()
        _evidence_worker.start()
    except Exception as e:  # noqa: BLE001
        print(f"[flood-service] 증거 수집 시작 실패: {str(e)[:120]}")
    try:
        from .event_sync import EventSync, _sync_flood
        _event_sync = EventSync(store, sync_fns=(_sync_flood,))
        _event_sync.start()
    except Exception as e:  # noqa: BLE001
        print(f"[flood-service] event_sync 시작 실패: {str(e)[:120]}")
    # config_rev LISTEN — platform-shell(다른 프로세스)의 카메라·설정
    # 변경을 이 프로세스의 FloodPipelineRunner가 즉시 받는다
    # (core/config_rev_bridge.py, road-service와 같은 패턴).
    try:
        import threading

        from ..core import config_rev_bridge
        _config_rev_listener_stop = threading.Event()
        _config_rev_listener_thread = threading.Thread(
            target=config_rev_bridge.listen_loop,
            args=(_config_rev_listener_stop,),
            name="config-rev-listen", daemon=True)
        _config_rev_listener_thread.start()
    except Exception as e:  # noqa: BLE001
        print(f"[flood-service] config_rev LISTEN 시작 실패: {str(e)[:120]}")
    yield
    runner.stop()
    if _event_sync is not None:
        try:
            _event_sync.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[flood-service] event_sync 종료 실패: {str(e)[:120]}")
    if _evidence_worker is not None:
        try:
            _evidence_worker.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[flood-service] 증거 수집 워커 종료 실패: {str(e)[:120]}")
    if _config_rev_listener_stop is not None:
        _config_rev_listener_stop.set()
    if _config_rev_listener_thread is not None:
        _config_rev_listener_thread.join(timeout=3.0)


_prime_settings()

app = FastAPI(title="UrbanGuard — 침수 서비스", lifespan=lifespan)
# platform-shell·crowd/road-service와 완전히 같은 미들웨어 순서.
app.add_middleware(AuthGuard)
app.add_middleware(ug_error_hook.ErrorCaptureMiddleware)


@app.get("/api/health")
def api_health():
    return {
        "status": "ok",
        # 2026-09-01 — 서비스 관리(관리자 전용, /admin/services)가 쓴다.
        "pid": os.getpid(),
        "uptime_sec": round(ug_service.uptime_seconds(), 1),
        "supervised": ug_service.is_supervised(),
        "water_segmentation": runner.water_backend_status(),
        "flood_corrupted_skips": runner.flood_corrupted_skips_status(),
        "block_count": len(runner.block_ids()),
    }


# R-01 whep_url 재계산 — 공유 함수(core/restream.py::
# rewrite_whep_urls_in_place)를 그대로 쓴다. main.py를 임포트하지
# 않는다 — 그 모듈 전체를 불러오면 또 하나의 옛(롤백 경로)
# PipelineRunner가 이 프로세스 안에서 함께 뜬다(crowd/road-service가
# 이미 겪은 함정과 같다).
from ..core.restream import rewrite_whep_urls_in_place as _rewrite_whep_urls


@app.get("/api/flood-risk")
def api_flood_risk(request: Request):
    """침수 카드 전체 — `main.py::api_risk()`의 침수 전용판."""
    blocks = store.all()
    _rewrite_whep_urls(blocks, request.url.hostname)
    return {"blocks": blocks}


@app.get("/api/flood-risk/{block_id}")
def api_flood_risk_block(block_id: str, request: Request):
    s = store.get(block_id)
    if not s:
        return {"error": "not found", "block_id": block_id}
    _rewrite_whep_urls([s], request.url.hostname)
    return s


@app.get("/api/flood-history")
def api_flood_history():
    return {"blocks": store.all_history()}


@app.get("/api/stream/flood-risk")
async def api_stream_flood_risk(request: Request):
    """침수 상황판 실시간 갱신 — `main.py::api_stream_risk()`의 침수
    전용판(§Phase 4 상세 설계 "SSE" 항목)."""
    host = request.url.hostname

    def _rewrite(data: dict) -> None:
        _rewrite_whep_urls(data.get("blocks", []), host)

    return StreamingResponse(sse.event_stream(store, on_payload=_rewrite),
                             media_type="text/event-stream",
                             headers=sse.HEADERS)


# ⚠️ 2026-08-31 — `/api/record/{block_id}`는 이 서비스로 옮기지 않는다.
# DB에서 카메라 소스만 읽으면 되는 순수 CCTV 녹화 기능이라 침수·교통
# 어느 쪽 소유도 아니다 — platform-shell에 그대로 두고, `runner.
# current_blocks()`(인메모리) 의존만 DB 조회로 바꾼다(§Phase 4 상세
# 설계 "이동하는 라우트" 참고, 커트오버 시점에 함께 처리).
