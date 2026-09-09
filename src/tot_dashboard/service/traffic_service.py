"""UrbanGuard — 교통위험 독립 서비스 (Phase 4, 2026-08-31 분리).

API 게이트웨이 도입 계획(``C:\\Users\\전태건\\.claude\\plans\\
ticklish-discovering-pine.md``) Phase 4 산출물. `service/runner.py::
PipelineRunner`에서 교통·기상 부분만 잘라낸 `service/runner_traffic.py::
TrafficPipelineRunner`를 이 별도 프로세스(포트 8037)에서 돌린다.

`/traffic` 화면(HTML)은 여전히 platform-shell이 서빙한다 — crowd/road·
flood-service와 같은 이유. `/api/blocks`도 DB 직접 조회라 그대로 둔다.

이 서비스는 옛 `/api/risk*`·`/api/stream/risk`·`/api/report/{block_id}`
이름을 **그대로 물려받는다** — 이 이름들이 이미 "교통·기상 판정"을
가리키고 있었고(코드로 확인, `api_report`가 읽는 필드가 전부 교통
전용이다), 침수 쪽만 새 이름(`/api/flood-risk*`)을 쓴다.
"""
from __future__ import annotations

import base64
import json
import os
import time
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
from ..traffic_weather.agents.vlm_situation import VlmSituationAgent
from ..traffic_weather.report_generator import build_briefing_from_snapshot
from . import sse
from .store import RiskStore

load_dotenv(PROJECT_ROOT / ".env")


def _load_blocks() -> list[dict]:
    """`flood_service.py::_load_blocks()`와 같은 패턴이되 교통위험만 본다."""
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
            blocks = _cams.continuous_blocks(db, _Dom.TRAFFIC.value)
            if blocks:
                print(f"[traffic-service] DB에서 상시 교통 카메라 {len(blocks)}개 로드")
            else:
                print("[traffic-service] 상시 교통으로 지정된 카메라가 없습니다 "
                      "— S-80에서 지정하세요")
            return blocks
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[traffic-service] DB 조회 실패, 빈 목록으로 시작: {str(e)[:120]}")
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
        print(f"[traffic-service] DB 설정 로드 실패, 기본값 사용: {str(e)[:120]}")


store = RiskStore()

from .runner_traffic import TrafficPipelineRunner  # noqa: E402

runner = TrafficPipelineRunner(
    store, _load_blocks(),
    fps=float(os.environ.get("TOT_FPS", "5")),
    use_vlm=os.environ.get("TOT_USE_VLM", "0") == "1",
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
        print(f"[traffic-service] 증거 수집 시작 실패: {str(e)[:120]}")
    try:
        from .event_sync import EventSync, _sync_traffic
        _event_sync = EventSync(store, sync_fns=(_sync_traffic,))
        _event_sync.start()
    except Exception as e:  # noqa: BLE001
        print(f"[traffic-service] event_sync 시작 실패: {str(e)[:120]}")
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
        print(f"[traffic-service] config_rev LISTEN 시작 실패: {str(e)[:120]}")
    yield
    runner.stop()
    if _event_sync is not None:
        try:
            _event_sync.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[traffic-service] event_sync 종료 실패: {str(e)[:120]}")
    if _evidence_worker is not None:
        try:
            _evidence_worker.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[traffic-service] 증거 수집 워커 종료 실패: {str(e)[:120]}")
    if _config_rev_listener_stop is not None:
        _config_rev_listener_stop.set()
    if _config_rev_listener_thread is not None:
        _config_rev_listener_thread.join(timeout=3.0)


_prime_settings()

app = FastAPI(title="UrbanGuard — 교통위험 서비스", lifespan=lifespan)
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
        "block_count": len(runner.block_ids()),
    }


# R-01 whep_url 재계산 — flood_service.py와 같은 공유 함수를 쓴다
# (core/restream.py::rewrite_whep_urls_in_place). main.py를 임포트하지
# 않는다(옛 PipelineRunner가 또 하나 뜬다).
from ..core.restream import rewrite_whep_urls_in_place as _rewrite_whep_urls


@app.get("/api/risk")
def api_risk(request: Request):
    blocks = store.all()
    _rewrite_whep_urls(blocks, request.url.hostname)
    return {"blocks": blocks}


@app.get("/api/risk/{block_id}")
def api_risk_block(block_id: str, request: Request):
    s = store.get(block_id)
    if not s:
        return {"error": "not found", "block_id": block_id}
    _rewrite_whep_urls([s], request.url.hostname)
    return s


@app.get("/api/history")
def api_history():
    return {"blocks": store.all_history()}


@app.get("/api/stream/risk")
async def api_stream_risk(request: Request):
    host = request.url.hostname

    def _rewrite(data: dict) -> None:
        _rewrite_whep_urls(data.get("blocks", []), host)

    return StreamingResponse(sse.event_stream(store, on_payload=_rewrite),
                             media_type="text/event-stream",
                             headers=sse.HEADERS)


@app.get("/api/report/{block_id}")
def api_report(block_id: str):
    """`main.py::api_report()`를 그대로 옮긴다 — 이 보고서가 읽는 필드
    (risk_name/level/severity/mean_speed/speed_drop/state/queue_len/
    stalled)는 전부 교통 전용이라 처음부터 이 도메인 소유였다."""
    s = store.get(block_id)
    if not s:
        return {"error": "not found", "block_id": block_id}
    img = None
    b64 = s.get("snapshot")
    if b64:
        try:
            img = base64.b64decode(b64.split(",")[-1])
        except Exception:  # noqa: BLE001
            img = None
    facts = {
        "location": s.get("name"), "t_sec": s.get("t_sec"),
        "risk_name": s.get("risk_name"), "risk_code": s.get("risk_code"),
        "level": s.get("level"), "severity": s.get("severity"), "score": s.get("score"),
        "rain_mm_h": s.get("rain_mm_h"), "intensity": s.get("intensity"),
        "mean_speed": round(s.get("mean_speed") or 0.0, 1),
        "speed_drop": round((s.get("speed_drop") or 0.0) * 100),
        "state": s.get("state"), "queue_len": s.get("queue_len"), "stalled": s.get("stalled"),
        "drivers": s.get("drivers"), "recommendation": s.get("recommendation"),
    }
    agent = VlmSituationAgent(use_vlm=True, location=s.get("name", block_id))
    narrative = agent.narrate_report(facts, image=img)
    md = build_briefing_from_snapshot(s, narrative=narrative)
    out_dir = PROJECT_ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    # ⚠️ 응답 형태·파일명 규칙을 main.py 옛 구현과 정확히 맞춘다 —
    # static/app.js가 `j.markdown`·`j.narrative_source`를 그대로 읽는다
    # (report_md/saved_path 같은 다른 이름을 쓰면 화면이 깨진다).
    fname = f"briefing_{block_id}_{time.strftime('%Y%m%d_%H%M%S')}.md"
    (out_dir / fname).write_text(md, encoding="utf-8")
    return {"block_id": block_id, "name": s.get("name"), "markdown": md,
            "filename": fname, "narrative_source": "gemini-vlm" if narrative else "rule"}


@app.get("/api/traffic/roi-flow/propose")
def api_traffic_roi_flow_propose(camera_id: str):
    """통행 방향 자동 생성 제안(교통 로드맵 Phase 4-A) — `routes_cameras.py`
    의 옛 ``/settings/cameras/{id}/roi/traffic/flow/propose``를 그대로
    옮긴 것이다. 그 URL은 `/settings/cameras/*`(카메라 CRUD, DB만 봄이라
    platform-shell에 그대로 두는 원칙)에 걸려 있었지만, 정작 이 핸들러는
    **실행 중인 추적기의 인메모리 상태**(``TrafficPipelineRunner.
    traffic_flow_samples()``)가 있어야 답할 수 있어 traffic-service
    소유가 맞다 — 그래서 새 경로를 받았다(권한 규칙은
    ``core/guard.py``에 함께 추가).

    ⚠️ 인증·권한은 ``AuthGuard``가 라우트 진입 전에 이미 확인했다
    (경로 규칙표에 SETTINGS_OPS×VIEW×TRAFFIC 등록) — 옛 라우트의
    ``require_page``·``_require_domain``과 동등하다.
    """
    from ..core import cameras as C
    from ..core.db import get_session
    from ..core.roles import Domain as _Dom
    from ..traffic_weather.perception import flow_learning as FL

    db = get_session()
    try:
        cam = C.get(db, camera_id)
        if cam is None:
            return {"ok": False, "errors": ["카메라를 찾을 수 없습니다."]}
        live = runner.traffic_flow_samples(camera_id)
        if live is None:
            return {"ok": False, "errors": [
                "이 카메라는 지금 상시 처리 중이 아닙니다 — 상시 탐지를 "
                "켠 뒤 차량이 지나가는 모습을 얼마간 관찰해야 합니다."]}
        samples, live_wh = live
        if not samples:
            return {"ok": False, "errors": [
                "아직 쌓인 관측이 없습니다. 잠시 뒤 다시 시도하세요."]}
        roi = C.roi_of(db, camera_id, _Dom.TRAFFIC.value)
    finally:
        db.close()
    stored_wh = (roi.get("frame_width") or 0, roi.get("frame_height") or 0)
    if not (stored_wh[0] and stored_wh[1]):
        return {"ok": False, "errors": [
            "정지영상 해상도를 알 수 없습니다 — ROI를 한 번이라도 "
            "저장한 뒤 다시 시도하세요."]}
    # ⚠️ 실시간 탐지 프레임 해상도와 ROI 편집기가 쓰는 "정지영상" 해상도가
    # 다를 수 있다(이 프로젝트가 반복해 겪은 함정) — 변위 벡터를 정지영상
    # 기준으로 옮긴 뒤 계산해야 화면에 그린 화살표가 실제 자리를 가리킨다.
    if live_wh and (int(live_wh[0]), int(live_wh[1])) != stored_wh:
        from ..common.roi import scale_polygons
        as_polys = [[[x0, y0], [x1, y1]] for x0, y0, x1, y1 in samples]
        scaled = scale_polygons(as_polys, live_wh, stored_wh)
        samples = [(p[0][0], p[0][1], p[1][0], p[1][1]) for p in scaled]
    result = FL.propose_arrows(samples, stored_wh)
    return {"ok": True, "sample_count": len(samples), **result}
