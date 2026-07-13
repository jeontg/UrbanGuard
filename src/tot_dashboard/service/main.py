"""Unified FastAPI dashboard — merges flood3's ``service/main.py`` (multi-block
live flood/traffic risk) and SAM's ``app/main.py`` (crowd case viewer +
SOLAPI notifications) into one app (docs/integration_plan.md Phase 7).

Run:
  uvicorn tot_dashboard.service.main:app --port 8000
  (or the ``tot-service`` console script, see pyproject.toml)

Env vars:
  TOT_USE_VLM=1        -> use Gemini VLM for traffic_weather interpretation (needs GEMINI_API_KEY).
                          Default = rule-based fallback.
  TOT_FPS               -> pipeline fps (default 5)
  CROWD_DATA_ROOT       -> override data/cases/ location (default: <repo>/data/cases)
"""
from __future__ import annotations

import base64
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..common import cctv_capture as cap
from ..common.case_archive.catalog import CaseCatalog
from ..common.config import PROJECT_ROOT
from ..common.notifier import AlertNotifier, DuplicateNotificationError, NotificationConfigurationError
from ..traffic_weather.agents.vlm_situation import VlmSituationAgent
from ..traffic_weather.report_generator import build_briefing_from_snapshot
from .runner import PipelineRunner
from .store import RiskStore

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

CROWD_DATA_ROOT = Path(
    os.environ.get("CROWD_DATA_ROOT", PROJECT_ROOT / "data" / "cases")
).resolve()


def _load_blocks() -> list[dict]:
    # TOT_BLOCKS_PATH lets tests (and alternate deployments) point at a
    # different blocks.json without touching the real one -- e.g. to avoid
    # connecting to flood3's live Busan HLS streams during automated tests.
    override = os.environ.get("TOT_BLOCKS_PATH")
    path = Path(override) if override else PROJECT_ROOT / "configs" / "blocks.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)["blocks"]
    return [{"id": "BLOCK-A", "name": "도심 블록-A",
             "coordinates": {"lat": 35.1535, "lng": 129.0608},
             "dept": "도로관리과", "source": {"type": "synthetic"},
             "rain": {"peak": 26.0, "period": 48.0, "phase": 0.0}}]


BLOCKS = _load_blocks()
store = RiskStore()
catalog = CaseCatalog(CROWD_DATA_ROOT)
notifier = AlertNotifier()
runner = PipelineRunner(
    store, BLOCKS,
    fps=float(os.environ.get("TOT_FPS", "5")),
    use_vlm=os.environ.get("TOT_USE_VLM", "0") == "1")


@asynccontextmanager
async def lifespan(app: FastAPI):
    runner.start()
    yield
    runner.stop()


app = FastAPI(title="tot_dashboard — 통합 도로위험·군중안전 대시보드", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"blocks": BLOCKS})


@app.get("/api/health")
def api_health():
    cases = catalog.list_cases()
    return {
        "status": "ok",
        "block_count": len(BLOCKS),
        "case_count": len(cases),
        "data_root": str(CROWD_DATA_ROOT),
        "notifications": notifier.status(),
    }


# ── flood/traffic_weather: multi-block live risk (from flood3) ──────────────
@app.get("/api/blocks")
def api_blocks():
    return {"blocks": [{"block_id": b["id"], "name": b["name"],
                        "coordinates": b["coordinates"]} for b in BLOCKS]}


@app.get("/api/risk")
def api_risk():
    """Current risk snapshot for every block (no images, lightweight)."""
    return {"blocks": store.all()}


@app.get("/api/risk/{block_id}")
def api_risk_block(block_id: str):
    """Single-block detail — camera snapshot + recent history."""
    s = store.get(block_id)
    return s or {"error": "not found", "block_id": block_id}


@app.get("/api/history")
def api_history():
    """Per-block risk-level history (for the timeline chart)."""
    return {"blocks": store.all_history()}


@app.get("/api/record/{block_id}")
def api_record(block_id: str, seconds: int = 10):
    """Record ``seconds`` of the live stream to an mp4 for download (reuses
    the ffmpeg-based cctv_capture helper)."""
    b = next((x for x in BLOCKS if x["id"] == block_id), None)
    if not b:
        return {"error": "block not found", "block_id": block_id}
    src = b.get("source") or {}
    url = src.get("url") or src.get("path")
    if not url:
        return {"error": "no stream (synthetic block / no URL configured)"}
    seconds = max(3, min(int(seconds), 30))
    out_dir = PROJECT_ROOT / "data" / "recordings"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{block_id}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    ok, msg = cap.capture_video(url, str(path), dur=seconds)
    if not ok or not path.exists():
        return {"error": f"recording failed: {msg}"}
    return FileResponse(path, media_type="video/mp4", filename=f"{b['name']}_{seconds}s.mp4")


@app.get("/api/report/{block_id}")
def api_report(block_id: str):
    """Generate an incident-briefing report (MD) — only the "종합 판단"
    narrative is Gemini-generated (on-demand, independent of the live VLM
    toggle); everything else is rule-fixed. Falls back to a rule sentence
    with no key. Also saved under data/reports/."""
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
    fname = f"briefing_{block_id}_{time.strftime('%Y%m%d_%H%M%S')}.md"
    (out_dir / fname).write_text(md, encoding="utf-8")
    return {"block_id": block_id, "name": s.get("name"), "markdown": md,
            "filename": fname, "narrative_source": "gemini-vlm" if narrative else "rule"}


# ── crowd: SAM3 case viewer + notifications (from SAM) ──────────────────────
class NotificationRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=64)
    channels: list[Literal["sms", "kakao"]] = Field(min_length=1, max_length=2)


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


@app.get("/api/notifications/status")
def api_notification_status():
    return notifier.status()


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


def run() -> None:
    """Console-script entry point (``tot-service``)."""
    import uvicorn
    uvicorn.run("tot_dashboard.service.main:app", host="127.0.0.1", port=8000)
