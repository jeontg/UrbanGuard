"""Unified FastAPI dashboard — merges flood3's ``service/main.py`` (multi-block
live flood/traffic risk) and SAM's ``app/main.py`` (crowd case viewer +
SOLAPI notifications) into one app (docs/integration_plan.md Phase 7).

⚠️ 2026-08-31 — API 게이트웨이 도입 Phase 1로 인파관리(SAM3 케이스 뷰어 +
알림)는 별도 서비스(``service/crowd_service.py``, 포트 8034)로 옮겨졌다.
이 모듈("platform-shell")은 그 외 전부(침수·교통위험·노면관리·인증·설정·
카메라·이벤트 등 공용 인프라)를 계속 담당한다. 상세: 계획서
``C:\\Users\\전태건\\.claude\\plans\\ticklish-discovering-pine.md``.

Run:
  uvicorn tot_dashboard.service.main:app --port 8000
  (or the ``tot-service`` console script, see pyproject.toml)

Env vars:
  TOT_USE_VLM=1        -> use Gemini VLM for traffic_weather interpretation (needs GEMINI_API_KEY).
                          Default = rule-based fallback.
  TOT_FPS               -> pipeline fps (default 5)
"""
from __future__ import annotations

import json
import threading
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..common import cctv_capture as cap
from ..common.case_archive.run_writer import RUNS_DIR, list_runs, load_run
from ..common.config import PROJECT_ROOT

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def _load_blocks_from_file() -> list[dict]:
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


def _load_blocks() -> list[dict]:
    """상시 탐지 대상 카메라 목록.

    관리자가 S-80에서 「침수 = 사용 + 상시」 또는 「교통위험 = 사용 + 상시」로
    지정한 카메라를 파이프라인이 돌린다(합집합 — 카메라 하나가 둘 다일 수도
    있다). 선택 탐지 카메라는 화면에서 고를 때만 분석한다.

    ⚠️ 2026-08-23 — 예전에는 침수 하나만 봤다. 그래서 침수는 미사용이고
    교통위험만 상시로 켠 카메라는 파이프라인에 아예 안 올라갔다(실사용 중
    발견 — 교통위험 상시 지정 후 재기동해도 실시간 관제에 안 보이던 문제).
    ``to_block_dict()``가 채우는 ``flood_enabled`` 플래그가 실제 침수 판정
    실행 여부를 가르므로, 이 목록에 침수 미사용 카메라가 섞여도 안전하다.

    DB를 읽지 못하면 기존 `blocks.json` 으로 내려간다 — 설정 하나 때문에
    탐지 서비스 전체가 기동하지 못하면 안 된다. 테스트가
    ``TOT_BLOCKS_PATH`` 를 지정한 경우에도 파일을 쓴다.
    """
    if os.environ.get("TOT_BLOCKS_PATH"):
        return _load_blocks_from_file()
    try:
        from ..core import cameras as _cams
        from ..core.db import get_session
        from ..core.roles import Domain as _Dom
        db = get_session()
        try:
            # 상시 목록 조합은 core/cameras.continuous_blocks_any() 한 곳에만
            # 둔다 — PipelineRunner 의 동적 재구성(_reconcile)도 같은 함수를 쓴다.
            blocks = _cams.continuous_blocks_any(
                db, (_Dom.FLOOD.value, _Dom.TRAFFIC.value))
            if blocks:
                print(f"[blocks] DB에서 상시 탐지 카메라 {len(blocks)}개 로드")
                return blocks
            print("[blocks] 상시 탐지로 지정된 카메라가 없습니다 "
                  "— S-80에서 지정하세요")
            return []
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[blocks] DB 조회 실패, blocks.json 사용: {str(e)[:120]}")
        return _load_blocks_from_file()


BLOCKS = _load_blocks()


_WEATHER_TTL_SEC = 600      # 관측이 10분 간격이라 더 자주 물어도 값이 같다
_weather_cache: dict[str, tuple[float, dict]] = {}


def _weather_card(board: list[dict], region: str) -> dict:
    """상황판에 띄울 기상 관측. **실패해도 상황판은 떠야 한다.**

    지점마다 부르면 요청이 지점 수만큼 늘어난다. **보고 있는 지역의 첫
    지점 하나**만 물어 그 지역 대표값으로 쓴다 — 격자가 5km 라 한 지역 안에서는
    큰 차이가 없다.

    캐시를 두는 이유는 두 가지다. 관측이 10분 간격이라 더 자주 물어도 같은
    값이고, 개발계정 한도가 1만 회/일이다.
    """
    card = {"available": False, "note": "", "place": "", "labels": {},
            "rain_mm": None, "base": ""}
    pick = next((b for b in board
                 if b.get("coordinates", {}).get("lat") is not None), None)
    if pick is None:
        card["note"] = "좌표가 있는 지점이 없어 기상을 조회하지 않았습니다."
        return card

    coords = pick["coordinates"]
    key = f'{region}|{round(coords["lat"], 2)},{round(coords["lng"], 2)}'
    hit = _weather_cache.get(key)
    if hit and (time.time() - hit[0]) < _WEATHER_TTL_SEC:
        return hit[1]

    card["place"] = pick.get("name") or pick.get("id") or ""
    try:
        snap = ug_weather.snapshot(coords["lat"], coords["lng"])
    except Exception as e:  # noqa: BLE001
        print(f"[weather] 기상 조회 실패: {str(e)[:120]}")
        card["note"] = "기상 정보를 가져오지 못했습니다."
        return card

    if not snap["available"]:
        # 키가 없거나 망이 막힌 것은 **고장이 아니다.** 안내만 남긴다.
        card["note"] = snap["errors"][0] if snap["errors"] else "기상 정보 없음"
        _weather_cache[key] = (time.time(), card)
        return card

    values = snap["values"]
    card.update(
        # 요약줄은 겹치는 항목(바람 성분 등)을 뺀 것으로 쓴다.
        available=True, labels=ug_weather.summary_labels(values),
        rain_mm=ug_weather.rainfall_mm(values),
        base=snap["base"],
        missing=ug_weather.missing_for_prediction(values),
        # 도메인마다 보는 항목이 다르다. 전부 늘어놓으면 무엇을 봐야 하는지
        # 알 수 없어, 도메인별로 추려 「왜 보는지」와 함께 준다.
        # ⚠️ 키 이름을 `items` 로 두면 안 된다 — Jinja 가 dict.items() 메서드로
        # 풀어 버려 반복이 터진다. 실제로 그렇게 새어 나갔다.
        domains=[{"key": d, "label": lab,
                  "why": ug_weather.DOMAIN_WHY.get(d, ""),
                  "rows": ug_weather.for_domain(d, values)}
                 for d, lab in (("flood", "침수"), ("crowd", "인파"),
                                ("road", "노면"))],
        # 한쪽만 실패했으면 그것도 알려 준다.
        partial=snap["errors"],
    )
    _weather_cache[key] = (time.time(), card)
    return card


def board_cameras() -> list[dict]:
    """**통합 상황판(S-01) 지도에 찍을 지점 — 등록된 전부.**

    ``BLOCKS`` 를 쓰지 않는 이유가 있다. BLOCKS 는 「침수 = 사용 + 상시」로
    지정한 카메라만 담는다. 그것을 통합 상황판 지도에 그대로 쓰면 **인파·노면
    전용 지점과 선택 분석 지점이 지도에서 통째로 사라진다.** 실제로 39대를
    등록해 두고 2대만 보이던 상태였다.

    상황판은 「무엇을 분석 중인가」가 아니라 「어디를 보고 있는가」를 그리는
    화면이다. 등록된 지점은 전부 나와야 한다 — 이벤트가 없는 지점을 「정상」
    으로 찍는 것과 같은 이유다(board_map.build_points 주석).

    DB를 읽지 못하면 BLOCKS 로 내려간다.
    """
    try:
        from ..core import cameras as _cams
        from ..core.db import get_session
        db = get_session()
        try:
            out = [_cams.to_block_dict(c)
                   for c in _cams.list_all(db, active_only=True)
                   if c.lat is not None and c.lng is not None]
            if out:
                return out
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[board] 상황판 지점 목록 조회 실패: {str(e)[:120]}")
    return BLOCKS


def flood_cameras() -> list[dict]:
    """침수·교통위험 화면에 보여 줄 카메라 — S-80에서 **「사용」으로 지정한 전부**.

    ``BLOCKS``(상시 분석 대상)와 일부러 나눴다. 상시만 보여 주면 관리자가
    S-80에서 「선택」으로 지정한 지점이 화면에서 아예 사라져, 설정이 반영되지
    않은 것처럼 보인다. 목록에는 싣되 **분석 방식을 함께 표시**한다.

    ⚠️ 2026-08-23 — 이름은 여전히 "flood_cameras" 지만(호출부가 많아 이번
    범위에서는 개명하지 않았다 — 개명 자체가 별도 과제), **/flood 화면과
    /traffic 화면이 이 함수의 결과(``window.INITIAL_BLOCKS``)를 함께 쓴다**
    (실사용 중 발견 — 교통위험만 사용으로 지정한 카메라가 그동안 이 목록에
    없어 「선택」 카드로도 뜨지 못했다). 그래서 침수 **또는** 교통위험 중
    하나라도 「사용」이면 포함한다(합집합).

    DB를 읽지 못하면 상시 목록(BLOCKS)으로 내려간다.

    ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 예전에는 이 폴백에서 "상시"
    표시를 **지금 실제로 도는 지점**(``runner.block_ids()``, 동적
    재구성 반영)으로 다시 확인했다. 침수·교통이 별도 프로세스가 되며
    이 프로세스엔 그 러너가 없다 — 그런데 ``BLOCKS`` 자체가 이미
    ``continuous_blocks_any()``(상시 지정된 것만)의 결과라, 이 폴백
    경로(DB 재조회조차 실패한 드문 경우)에서는 ``BLOCKS`` 에 있다는
    것 자체가 곧 "상시"라는 뜻이다 — 다시 확인할 필요가 없다.
    """
    try:
        from ..core import cameras as _cams
        from ..core.db import get_session
        from ..core.roles import Domain as _Dom
        db = get_session()
        try:
            seen: dict[str, object] = {}
            for dom in (_Dom.FLOOD.value, _Dom.TRAFFIC.value):
                for c in _cams.for_domain(db, dom):
                    seen.setdefault(c.id, c)
            out = []
            for c in seen.values():
                frow = c.domain_row(_Dom.FLOOD.value)
                trow = c.domain_row(_Dom.TRAFFIC.value)
                continuous = bool((frow and frow.continuous) or (trow and trow.continuous))
                out.append({
                    "id": c.id, "name": c.name, "dept": c.dept,
                    "coordinates": {"lat": c.lat, "lng": c.lng},
                    "mode": "continuous" if continuous else "selective",
                    "has_roi": bool(c.roi_row(_Dom.FLOOD.value)),
                    "has_traffic_roi": bool(c.roi_row(_Dom.TRAFFIC.value)),
                })
            if out:
                return out
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[blocks] 침수 카메라 목록 조회 실패: {str(e)[:120]}")
    return [{"id": b["id"], "name": b["name"], "dept": b.get("dept", ""),
             "coordinates": b["coordinates"], "mode": "continuous",
             "has_roi": True} for b in BLOCKS]


# ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 침수·교통위험이 별도 프로세스
# (flood_service.py 포트 8036, traffic_service.py 포트 8037)로 이관됐다.
# 인파관리(Phase 1)·노면관리(Phase 2)에 이어, 이제 이 프로세스
# (platform-shell)는 **실시간 탐지를 하나도 직접 돌리지 않는다** —
# `store`(RiskStore)·`runner`(PipelineRunner)·증거영상 훅·event_sync가
# 전부 그 두 새 서비스로 옮겨갔다. 옛 구현은 롤백 참고용으로
# `service/runner.py`에 그대로 남아 있다(사용처는 없음).


@asynccontextmanager
async def lifespan(app: FastAPI):
    # CCTV 재배포 허브(MediaMTX, 2026-08-28) — 기동 시 1회, DB의 카메라
    # 전체를 MediaMTX에 채운다(``restream.enabled``가 꺼져 있으면 내부에서
    # 바로 반환). 카메라 수가 많으면 호스트별 순차 등록으로 수십 초 걸릴 수
    # 있어(``core/restream.py::sync_all_paths`` 주석 참고) 앱 기동을 막지
    # 않도록 별도 스레드에서 실행한다 — 끝나기 전에는 재배포 URL로 치환된
    # 카메라가 아직 원본 직결로 남아 있을 뿐, 폴백 없음 정책과 무관하게
    # 안전하다.
    # ffmpeg 릴레이 감시(2026-08-29, 같은 날 후속) — MediaMTX 자신의 HLS
    # 디먹서가 일부 카메라 영상을 손상시키는 문제를 우회하려고, 화이트
    # 리스트(``restream.relay_ids``)에 오른 카메라는 ffmpeg가 원본을
    # 대신 읽어 MediaMTX에 발행한다(``core/ffmpeg_relay.py``). 감시
    # 스레드를 기동 시 1회 띄운다 — 아직 릴레이 프로세스가 하나도 없어도
    # 무해하며, 아래 sync_all_paths()가 만드는 프로세스를 그대로 지켜본다.
    try:
        from ..core import ffmpeg_relay as _ffmpeg_relay
        _ffmpeg_relay.start_watchdog()
    except Exception as e:  # noqa: BLE001
        print(f"[ffmpeg_relay] 감시 스레드 시작 실패: {str(e)[:120]}")
    # 상시 화질 감시(2026-08-30) — 손상 카메라를 지금까지 "사용자가
    # 우연히 제보"로만 발견해 온 것을 없애려고, 재배포 경로를 저빈도로
    # 스스로 재는 스캐너를 둔다(core/video_quality.py). 기본 꺼짐
    # (video_quality.enabled=0)이라 관리자가 켜기 전엔 무해하다.
    try:
        from ..core import video_quality as _video_quality
        _video_quality.start_scanner()
    except Exception as e:  # noqa: BLE001
        print(f"[video_quality] 스캔 스레드 시작 실패: {str(e)[:120]}")
    # 이벤트 자동 보류(2026-09-02, 사용자 요청) — 재탐지 없이 오래
    # 방치된 미해결 이벤트를 "자동 보류"로 옮긴다(종결 아님, `core/
    # events.py::AUTO_HELD` 참고). platform-shell 하나에서만 기동 —
    # 이벤트는 공유 DB 하나에 모이므로 도메인 서비스마다 돌릴 이유가
    # 없다.
    try:
        from ..core import events as _ug_events
        _ug_events.start_auto_hold_watchdog()
    except Exception as e:  # noqa: BLE001
        print(f"[events] 자동 보류 감시 스레드 시작 실패: {str(e)[:120]}")
    try:
        from ..core import restream as _restream
        from ..core.db import get_session as _get_restream_session

        def _sync_restream_paths_bg() -> None:
            db = _get_restream_session()
            try:
                result = _restream.sync_all_paths(db)
                if result["total"] or result["excluded"]:
                    print(f"[restream] MediaMTX 경로 동기화 완료 — "
                          f"{result['ok']}/{result['total']} 성공"
                          + (f", 제외 {len(result['excluded'])}곳" if result["excluded"] else "")
                          + (f", 실패 {len(result['failed'])}곳: {result['failed'][:5]}" if result["failed"] else ""))
            except Exception as e:  # noqa: BLE001
                print(f"[restream] MediaMTX 경로 동기화 실패: {str(e)[:120]}")
            finally:
                db.close()

        threading.Thread(target=_sync_restream_paths_bg, daemon=True,
                         name="restream-sync").start()
    except Exception as e:  # noqa: BLE001
        print(f"[restream] 경로 동기화 시작 실패: {str(e)[:120]}")
    yield
    _shutdown_workers()


def _shutdown_workers() -> None:
    """상시 탐지·동기화 스레드를 모두 세운다.

    종료(lifespan)와 **재기동**(S-01 버튼)이 같이 쓴다. 재기동은 정상 종료
    경로를 타지 않으므로(:mod:`..core.service_control` 참고) 여기를 직접
    불러 줘야 스트림 연결과 임시 파일이 남지 않는다.

    ⚠️ 2026-08-31(Phase 4) — 이 프로세스는 더 이상 상시 탐지 워처를 갖지
    않는다(위 모듈 docstring 참고) — 지금은 아무것도 안 하지만, 재기동
    버튼 코드가 이 함수를 그대로 부르므로 시그니처를 유지한다.
    """
    return


def _watcher_count() -> int:
    """지금 돌고 있는 상시 탐지 워처 수. 재기동하면 **이만큼이 멎는다.**

    ⚠️ 2026-08-31(Phase 4) — 침수·교통위험까지 별도 프로세스로 옮겨가며
    이 프로세스는 실시간 탐지 워처를 하나도 안 갖는다 — 그래서 항상 0.
    재기동해도 4개 도메인 탐지가 전혀 멎지 않는다는 뜻이며, 이것이
    독립 배포의 최종 실질적 효과다.
    """
    return 0


app = FastAPI(title="UrbanGuard — 통합 도시안전 관제", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# --- UrbanGuard 제품 계층 (docs/ui_design_spec.md v4) -------------------------
from ..core import branding as ug_branding  # noqa: E402
from ..core import roles as UG_ROLES  # noqa: E402
from ..core import settings as ug_settings  # noqa: E402
from ..core import service_control as ug_service  # noqa: E402
from ..core.auth import (LoginRequired, client_ip, get_db,  # noqa: E402
                         login_redirect, menu_for, require)
from ..core.guard import RELOCATED, AuthGuard  # noqa: E402
from ..core.security import MAX_FAILED, MIN_PASSWORD_LEN  # noqa: E402
from ..core import events as ug_events  # noqa: E402
from ..core import regions as ug_regions  # noqa: E402
from ..core import weather_sources as ug_weather  # noqa: E402
from . import board_map, event_sync  # noqa: E402
from ..core import notifications as ug_notify  # noqa: E402
from ..core import error_hook as ug_error_hook  # noqa: E402
from ..core import errors as ug_errors  # noqa: E402
from ..core import sop as ug_sop  # noqa: E402
from . import (routes_account, routes_admin, routes_analytics,  # noqa: E402
               routes_auth, routes_cameras, routes_config, routes_disclosure,
               routes_evidence,
               routes_handover,
               routes_errors, routes_events, routes_facility,
               routes_hazard_sop, routes_levels, routes_multiview,
               routes_notify,
               routes_reports, routes_server, routes_services, routes_settings,
               routes_signup, routes_sop, routes_training)

# 기관명은 DB 설정값이다 — 부울경 타 지자체 확장 시 코드 수정이 없어야 한다(P0-5).
# 환경변수는 DB에 값이 없을 때의 초기값으로만 쓴다.
app.state.org_name = os.environ.get("URBANGUARD_ORG_NAME", "부산광역시")
# 운영은 HTTPS 전제. 개발(HTTP)에서도 쿠키가 붙도록 기본은 꺼 두고 배포 시 켠다.
app.state.cookie_secure = os.environ.get("URBANGUARD_COOKIE_SECURE", "0") == "1"

# ``relocated=RELOCATED`` — platform-shell 전용. 침수·교통위험·인파관리·
# 노면관리로 옮겨간 API를 직접(게이트웨이 우회) 두드리면 404 대신 게이트웨이로
# 돌려보낸다(2026-09-01 실사용자 제보). 다른 4개 서비스는 이 인자 없이
# `AuthGuard`를 그대로 쓴다 — 이 경로들이 바로 **자기 자신**의 경로이기
# 때문에 여기서 리다이렉트하면 안 된다(core/guard.py::AuthGuard 참고).
app.add_middleware(AuthGuard, relocated=RELOCATED)
# 오류 수집은 **가드보다 바깥**이어야 한다. 나중에 붙인 미들웨어가 바깥쪽이므로
# 이 순서를 지키면 가드 자체에서 난 오류까지 잡힌다.
app.add_middleware(ug_error_hook.ErrorCaptureMiddleware)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return login_redirect(request)


def _static_version(filename: str) -> int:
    # Cache-busting token for /static/<filename> -- browsers were caching
    # app.js aggressively (StaticFiles sets no explicit no-cache headers),
    # which made front-end edits invisible without a manual hard-refresh.
    # Tying the query string to the file's own mtime fixes this automatically.
    return int((BASE_DIR / "static" / filename).stat().st_mtime)


def base_ctx(request: Request, user=None) -> dict:
    """모든 화면 공통 컨텍스트. 미들웨어가 넣어 둔 request.state.user 를 쓴다."""
    u = user if user is not None else getattr(request.state, "user", None)
    role = getattr(u, "role", None)
    # DB 설정이 없으면(첫 기동·DB 미가동) 기본값으로 조용히 내려간다 —
    # 설정 하나 때문에 관제 화면이 안 뜨면 안 된다.
    board_bg = ug_settings.get(ug_settings.KEY_BOARD_BG)
    org = ug_settings.get(ug_settings.KEY_ORG_NAME) or app.state.org_name
    theme = ug_settings.derive_theme(board_bg)
    solution = (ug_settings.get(ug_settings.KEY_SOLUTION_NAME)
                or ug_settings.DEFAULTS[ug_settings.KEY_SOLUTION_NAME])
    logo_file = ug_settings.get(ug_settings.KEY_LOGO_FILE)
    return {
        "org_name": org,
        "solution_name": solution,
        # 업로드 로고가 없으면 배경 밝기에 맞는 기본 로고가 나온다.
        "logo_url": ug_branding.logo_url(logo_file, theme["logo"], "ui"),
        "login_logo_url": ug_branding.logo_url(logo_file, theme["logo"], "tight"),
        "theme": theme,
        "user": u,
        "role_label": UG_ROLES.ROLE_LABELS.get(UG_ROLES.Role(role), "") if role else "",
        "menu": menu_for(u),
        "max_failed": MAX_FAILED,
        "min_password_len": MIN_PASSWORD_LEN,
        "app_js_version": _static_version("app.js"),
        "styles_css_version": _static_version("styles.css"),
        "ug_css_version": _static_version("urbanguard.css"),
        "active": "",
    }


def _prime_settings() -> None:
    """기동 시 DB 설정을 캐시에 올린다. 실패해도 서비스는 떠야 한다."""
    try:
        from ..core.db import get_session
        db = get_session()
        try:
            ug_settings.load_all(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[settings] DB 설정 로드 실패, 기본값 사용: {str(e)[:120]}")


def _prime_error_catalog() -> None:
    """기본 오류 코드를 심고, 백그라운드 오류 수집을 켠다 (S-92).

    DB가 아직 없어도 서비스는 떠야 하므로 실패는 삼킨다 — 다만 그때는 오류
    이력도 남지 않는다는 뜻이므로, 화면이 비어 있다고 「오류가 없다」로 읽으면
    안 된다(설치 가이드에 명시).
    """
    try:
        from ..core.db import get_session
        db = get_session()
        try:
            n = ug_errors.seed_builtin(db)
            if n:
                print(f"[errors] 기본 오류 코드 {n}건 등록")
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[errors] 기본 오류 코드 등록 실패: {str(e)[:120]}")
    # 상시 탐지 워처·분석 파이프라인의 ERROR 로그를 오류 이력으로 옮긴다.
    ug_error_hook.install()


def _prime_sop() -> None:
    """디지털 SOP 기본 뼈대를 심는다 (S-86).

    ⚠️ 심는 내용은 **어느 기관의 행동매뉴얼도 아니다.** 「이 칸에 무엇을 적어야
    하는가」를 보여 주는 뼈대이며, 화면은 이것을 「기본안 n/n」으로 표시해
    바뀌지 않았음을 드러낸다(:mod:`..core.sop` 머리말).

    이미 있는 단계는 덮어쓰지 않는다 — 기관이 고친 내용이 재기동마다 되돌아가면
    아무도 고치지 않는다.
    """
    try:
        from ..core.db import get_session
        db = get_session()
        try:
            n = ug_sop.seed_builtin(db)
            if n:
                db.commit()
                print(f"[sop] 기본 SOP 단계 {n}건 등록 (기관 매뉴얼로 교체 필요)")
            # 위험유형별 단계·매핑(S-98). 어휘가 먼저 심어져 있어야 걸린다.
            ns, nm = ug_sop.seed_hazard_sop(db)
            if ns or nm:
                db.commit()
                print(f"[sop] 위험유형별 단계 {ns}건 · 매핑 {nm}건 등록")
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[sop] 기본 SOP 등록 실패: {str(e)[:120]}")


def _flow_warnings(db) -> list[dict]:
    """상황판에 띄울 선행 경고. **실패해도 상황판은 떠야 한다.**

    관계 표가 아직 없는 설치본(마이그레이션 전)에서 상황판 전체가 500 이 되면
    안 된다 — 관제 화면은 부가 기능이 죽어도 살아 있어야 한다.
    """
    if db is None:
        return []
    try:
        from ..core import relations as ug_rel
        return ug_rel.downstream_warnings(db)
    except Exception as e:  # noqa: BLE001
        print(f"[relations] 선행 경고 조회 실패: {str(e)[:120]}")
        return []


def _prime_vocabulary() -> None:
    """위험등급·위험유형 어휘를 심는다 (관계 모델 1단계).

    기관이 등급 이름을 고쳤으면 **덮어쓰지 않는다** — 오류 코드 사전·SOP 와
    같은 원칙이다. 재기동마다 되돌아가면 아무도 고치지 않는다.
    """
    try:
        from ..core import vocabulary as ug_vocab
        from ..core.db import get_session
        db = get_session()
        try:
            lv, ht = ug_vocab.seed_builtin(db)
            if lv or ht:
                db.commit()
                print(f"[vocab] 위험등급 {lv}건 · 위험유형 {ht}건 등록")
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[vocab] 어휘 등록 실패: {str(e)[:120]}")


_prime_settings()
_prime_error_catalog()
_prime_sop()
_prime_vocabulary()
# 화면에서 지정한 운영 모델을 분석기에 적용한다. 이게 없으면 재기동 후
# 코드 기본값으로 되돌아가, 설정은 남아 있는데 다른 모델이 도는 상태가 된다.
from ..core import model_ops as ug_model_ops  # noqa: E402
ug_model_ops.prime_from_settings()

routes_auth.register(app, templates, base_ctx)
routes_signup.register(app, templates, base_ctx)
routes_account.register(app, templates, base_ctx)
routes_events.register(app, templates, base_ctx)
routes_notify.register(app, templates, base_ctx)
routes_facility.register(app, templates, base_ctx)
routes_reports.register(app, templates, base_ctx)
routes_analytics.register(app, templates, base_ctx)
routes_admin.register(app, templates, base_ctx)
routes_errors.register(app, templates, base_ctx)
routes_disclosure.register(app, templates, base_ctx)
routes_sop.register(app, templates, base_ctx)
routes_levels.register(app, templates, base_ctx)
routes_hazard_sop.register(app, templates, base_ctx)
routes_multiview.register(app, templates, base_ctx)
routes_server.register(app, templates, base_ctx)
routes_services.register(app, templates, base_ctx)
routes_evidence.register(app, templates, base_ctx)
routes_handover.register(app, templates, base_ctx)
routes_settings.register(app, templates, base_ctx)
routes_config.register(app, templates, base_ctx)
routes_training.register(app, templates, base_ctx)
# ⚠️ 2026-08-31(Phase 4) — 예전에는 ROI 편집기 정지영상 미리보기용
# store와 통행 방향 자동 생성용 runner를 함께 넘겼다. 침수·교통이 별도
# 프로세스가 되며 이 프로세스엔 둘 다 없다 — 정지영상은 소스 직접 조회
# 폴백만 쓰고, 자동 생성은 traffic-service로 옮겼다(routes_cameras.py
# 참고).
routes_cameras.register(app, templates, base_ctx)

# 위험등급 → 배지 색 (styles.css 의 등급 색 체계와 맞춘다)
_LEVEL_BADGE = {"심각": "ug-badge--crit", "경계": "ug-badge--crit",
                "주의": "ug-badge--warn", "관심": "ug-badge--on"}
_ALERT_LEVELS = {"주의", "경계", "심각"}


def _traffic_summary() -> dict:
    """교통위험 도메인 요약 — 강우 × 감속·정지 판정(`level`)에서 온다.

    ★ 2026-08-21: 함수 이름이 원래 ``_flood_summary`` 였는데, 실제로 읽는
    값은 처음부터 **교통·기상 판정 등급**(`level`)이었다. 도메인을 나누며
    이름을 사실에 맞췄다. 침수 요약은 :func:`_flood_summary` 가 따로 낸다.

    ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 교통위험이 별도 프로세스
    (traffic-service)로 옮겨가며 이 프로세스는 더 이상 `RiskStore`를
    직접 못 읽는다. 인파(`CrowdObservation`)·노면(`road_history`)과
    같은 원칙으로 `core/live_state.py`(traffic-service가 1초 주기로
    upsert)를 대신 조회한다 — 서비스 하나가 죽어도 홈 화면은 "N초 전
    마지막 관측"을 정직하게 보여줄 수 있어, 오히려 더 견고해진다.
    """
    from ..core import live_state

    ids = [b["id"] for b in BLOCKS if b.get("traffic_enabled")]
    states = live_state.latest_by_camera(ids, "traffic") if ids else {}
    if states is None:
        # DB 자체를 못 읽었다 — "정상"으로 보이면 안 된다.
        return {"available": False, "detail": "교통위험 관측을 확인할 수 없습니다",
                "headline": "—", "color": "var(--muted)"}
    # 교통위험을 지정한 지점만 센다(_flood_summary()의 water_available과
    # 같은 이유, 2026-08-28) — 아니면 침수만 상시인 지점의 중립 판정
    # (「관심」)까지 "이상 없는 지점"으로 세어 지점 수가 부풀려진다.
    # ``available``은 traffic-service가 이번 틱에 실제로 판정을 돌렸는가다
    # (재배포 장애 등으로 못 돌린 카메라는 제외).
    rows = [s for s in states.values() if s.get("available")]
    alert = [s for s in rows if (s.get("level") or "") in _ALERT_LEVELS]
    if not rows:
        return {"available": False, "detail": "분석 대기 중", "headline": "—",
                "color": "var(--muted)"}
    # ⚠️ min() 이어야 한다 — 인덱스는 심각=0·경계=1·주의=2 순이라 "가장 작은
    #   인덱스"가 가장 심각한 등급이다. max() 를 쓰면 여러 등급이 섞여 있을
    #   때 가장 가벼운 등급(주의)을 "최고"라고 표시하는 반전 버그가 된다
    #   (2026-08-22 발견 — 아래 _flood_summary() 는 처음부터 min() 이었다).
    worst = min(alert, key=lambda s: ("심각", "경계", "주의").index(s["level"])
                if s.get("level") in ("심각", "경계", "주의") else 9, default=None) \
        if alert else None
    return {
        "available": True,
        "headline": f"{len(alert)}건" if alert else "정상",
        "detail": (f"최고 「{worst['level']}」 · 지점 {len(rows)}개소" if worst
                   else f"지점 {len(rows)}개소 이상 없음"),
        "color": "#e8843b" if alert else "#3fb950",
    }


# 침수 위험등급(RiskEngine 1~5) → 상황판 등급. event_sync 와 같은 표를 쓴다 —
# 홈 타일과 이벤트 목록이 다른 말을 하면 안 된다.
def _flood_summary() -> dict:
    """침수 도메인 요약 — 물 세그멘테이션 위험도에서 온다 (2026-08-21 신설).

    물 세그멘테이션이 도는 지점만 센다. 안 도는 지점을 「이상 없음」에
    합치면 **보지 않은 곳을 봤다고 말하는 것**이 된다.

    ⚠️ 2026-08-31(Phase 4) — `_traffic_summary()`와 같은 이유로
    `core/live_state.py`를 조회한다. flood-service가 이미 `level`을
    행안부 4단계(관심/주의/경계/심각)로 매핑해 저장해 두므로
    (`_FLOOD_GRADE_LEVEL`, `service/event_sync.py`와 같은 매핑을
    씀), 여기서 다시 등급을 변환할 필요가 없다.
    """
    from ..core import live_state

    ids = [b["id"] for b in BLOCKS if b.get("flood_enabled")]
    states = live_state.latest_by_camera(ids, "flood") if ids else {}
    if states is None:
        return {"available": False, "headline": "—",
                "detail": "침수 관측을 확인할 수 없습니다",
                "color": "var(--muted)"}
    rows = [s for s in states.values() if s.get("available")]
    if not rows:
        return {"available": False, "headline": "—",
                "detail": "물 세그멘테이션 미동작 (합성·폴백 소스)",
                "color": "var(--muted)"}
    levels = [s.get("level") or "" for s in rows]
    alert = [lv for lv in levels if lv in _ALERT_LEVELS]
    worst = min(alert, key=lambda lv: ("심각", "경계", "주의").index(lv)) \
        if alert else ""
    return {
        "available": True,
        "headline": f"{len(alert)}건" if alert else "정상",
        "detail": (f"최고 「{worst}」 · 관측 {len(rows)}개소" if worst
                   else f"관측 {len(rows)}개소 이상 없음"),
        "color": "#e8843b" if alert else "#3fb950",
    }


def _crowd_summary(*, recent_minutes: float = 5.0) -> dict:
    """인파관리 도메인 요약 (2026-08-22 다시 씀).

    ⚠️ **예전에는 실제 위험도를 전혀 안 읽었다.** `_crowd_analyzer is not
    None` 여부만 보고 항상 "동작 중"(초록)을 고정 표시했다 — 「패닉분산」
    (최고 심각도)이 진행 중이어도 홈 타일만 계속 초록이었다. 다른 3개
    타일이 실제 관측에서 등급을 계산하는 것과 달리 이 타일만 거짓을
    말하고 있었다(2026-08-22 전수점검에서 발견).

    `_crowd_analyzer.step()`을 다시 불러 재는 방법은 쓰지 않는다 —
    `/api/crowd/live` 가 이미 매 호출마다 그 분석기의 시간을 진행시키고
    있어, 여기서 또 부르면 시간축이 어긋난다(`event_sync.record_crowd_
    snapshot()` docstring 참고). 대신 상시 감시 워처
    (`continuous.CrowdContinuousWatcher`)가 이미 DB(`crowd_observations`)에
    쌓아 둔 **최근 관측**을 읽는다 — `/crowd` 화면을 아무도 안 보고 있어도
    상시 워처는 계속 돌기 때문에 값이 끊기지 않는다.

    ⚠️ 2026-08-31 — API 게이트웨이 Phase 1로 `_crowd_analyzer`가 이
    프로세스에서 아예 사라졌다(별도 서비스 crowd_service.py로 이관).
    이 함수는 원래도 그 분석기를 다시 부르지 않고 DB만 읽고 있었으므로,
    "분석기가 살아 있는가"를 더는 확인하지 않고 곧바로 DB를 조회한다 —
    계획서가 의도한 대로("홈 화면은 도메인 서비스 프로세스 상태에
    안 걸리게") 오히려 더 견고해졌다: 인파관리 서비스가 죽어 있어도
    이 타일은 DB에 남은 마지막 관측을 그대로 보여준다.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from ..core.db import get_session
    from ..core.models import CrowdObservation
    from .event_sync import _CROWD_SEVERITY_LEVEL

    db = get_session()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=recent_minutes)
        rows = db.execute(
            select(CrowdObservation.camera_id, func.max(CrowdObservation.severity))
            .where(CrowdObservation.observed_at >= cutoff,
                   CrowdObservation.failed.is_(False))
            .group_by(CrowdObservation.camera_id)
        ).all()
    finally:
        db.close()

    if not rows:
        return {"available": True, "headline": "관측 대기", "color": "var(--muted)",
                "detail": f"최근 {recent_minutes:.0f}분간 관측 없음"}

    worst_sev = max(int(sev or 0) for _cid, sev in rows)
    alert_cams = [cid for cid, sev in rows
                  if _CROWD_SEVERITY_LEVEL.get(int(sev or 0), "") in _ALERT_LEVELS]
    worst_level = _CROWD_SEVERITY_LEVEL.get(worst_sev, "")
    if alert_cams:
        return {
            "available": True, "headline": f"{len(alert_cams)}건",
            "detail": f"최고 「{worst_level}」 · 관측 {len(rows)}개소",
            "color": "#e8843b" if worst_level != "심각" else "#e5484d",
        }
    return {"available": True, "headline": "정상",
            "detail": f"관측 {len(rows)}개소 이상 없음", "color": "#3fb950"}


def _road_summary() -> dict:
    """노면 도메인 요약.

    예전에는 「개발 중 / 탐지 모델 학습데이터 확보 전」이 **고정값으로 박혀**
    있었다. 상시 순회가 붙어 실제로 탐지가 도는 지금은 침수·인파와 마찬가지로
    실제 상태를 읽어야 한다 — 상황판이 실제와 다른 말을 하면 상황판을 안 본다.

    ⚠️ 「이상 없음」은 **모델이 못 찾았다**는 뜻이기도 하다. 현재 모델은 부산
    CCTV에서 탐지 0건이므로(도메인 갭), 0건을 「손상 없음」으로 단정하지 않도록
    detail 에 그 사실을 남긴다.

    ⚠️ 2026-08-31 — API 게이트웨이 Phase 2로 ``road/results.py``의
    지점별 최신 결과가 이 프로세스에서 사라졌다(별도 서비스
    road_service.py로 이관). ``_crowd_summary()``와 같은 원칙으로,
    이 함수는 DB(``road_history.latest_by_camera()``, 실시간 관측마다
    이미 저장되고 있던 테이블)를 직접 읽는다 — road-service가 죽어
    있어도 마지막 관측을 그대로 보여준다. 「집중 감시 중」 상세문구는
    road-service 프로세스 안의 상태(``road_live.manager``)라 더는
    표시하지 않는다(틀린 값을 보여주는 것보다 안전).
    """
    from ..core import road_history

    cams = road_cameras()
    if not cams:
        return {"available": False, "headline": "대상 없음",
                "detail": "CCTV 관리(S-80)에서 노면 지점을 지정하세요",
                "color": "var(--muted)"}

    latest = road_history.latest_by_camera([c["id"] for c in cams]) or {}
    rows = []
    for c in cams:
        r = latest.get(c["id"])
        if r is None:
            rows.append({"analyzed": False, "failed": False,
                        "grade": None, "defect_count": 0})
        elif r.get("failed") or (r.get("frames_analyzed") or 0) <= 0:
            rows.append({"analyzed": False, "failed": True,
                        "grade": None, "defect_count": 0})
        else:
            rows.append({"analyzed": True, "failed": False,
                        "grade": r.get("grade"),
                        "defect_count": r.get("defect_count") or 0})
    analyzed = [r for r in rows if r["analyzed"]]
    failed = [r for r in rows if r["failed"]]

    if not analyzed:
        # 아직 한 바퀴도 못 돌았는지, 돌았는데 전부 실패했는지를 구분한다.
        if failed:
            return {"available": True, "headline": f"{len(failed)}개소 실패",
                    "detail": "영상을 받지 못해 분석하지 못했습니다",
                    "color": "#e5484d"}
        return {"available": False, "headline": "대기 중",
                "detail": f"지점 {len(cams)}개소 · 첫 순회 대기",
                "color": "var(--muted)"}

    defects = sum(r["defect_count"] for r in analyzed)
    # 등급은 1(정상)~4(심각). 큰 값이 나쁘다.
    worst = max((r["grade"] for r in analyzed if r["grade"]), default=None)
    detail = f"지점 {len(cams)}개소 중 {len(analyzed)}개소 점검"
    if failed:
        detail += f" · {len(failed)}개소 실패"

    if defects:
        return {"available": True, "headline": f"손상 {defects}건",
                "detail": detail,
                "color": "#e5484d" if (worst or 0) >= 4 else "#e8843b"}
    return {"available": True, "headline": "탐지 0건",
            "detail": detail + " · 손상 없음으로 단정 불가(모델 한계)",
            "color": "#3fb950" if not failed else "#d4a017"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db=Depends(get_db), region: str = "",
         sigungu: str = ""):
    """S-01 통합 상황판. 이벤트 큐는 events 테이블에서 읽는다.

    ``region``(시/도 키)과 ``sigungu`` 로 지도를 좁힐 수 있다. **거르는 것은
    지도뿐이고 이벤트 큐·건수는 그대로 둔다** — 다른 지역에서 위험이 올라온
    것을 필터 때문에 못 보면 관제가 아니다.
    """
    user = getattr(request.state, "user", None)
    scope = (user.domain_set if user and user.role == UG_ROLES.Role.MGR.value
             else None)
    situations = []
    try:
        for e in ug_events.list_events(db, status="active",
                                       allowed_domains=scope, limit=12):
            situations.append({
                "id": e.id,
                "time": e.detected_at.strftime("%H:%M"),
                "place": e.place_name or e.block_id or "—",
                "domain": UG_ROLES.DOMAIN_LABELS.get(
                    UG_ROLES.Domain(e.domain), e.domain)
                if e.domain in {d.value for d in UG_ROLES.Domain} else e.domain,
                "level": e.level,
                "status": ug_events.STATUS_LABELS.get(e.status, e.status),
                "elapsed": ug_events.elapsed_text(e),
                "badge": _LEVEL_BADGE.get(e.level, "ug-badge--off"),
            })
        ev_counts = ug_events.counts(db, scope)
    except Exception:  # noqa: BLE001
        # 이벤트 조회가 실패해도 상황판 자체는 떠야 한다.
        ev_counts = {ug_events.OPEN: 0, ug_events.IN_PROGRESS: 0,
                     ug_events.CLOSED: 0}
    # 지역 현황과 필터. 블록에 이미 행정구역이 실려 있어 재조회하지 않는다.
    # **등록된 지점 전부**를 쓴다 — BLOCKS(침수 상시)만 쓰면 지도가 텅 빈다.
    board = board_cameras()
    region = (region or "").strip()
    sigungu = (sigungu or "").strip()
    region_summary: dict[str, dict] = {}
    for b in board:
        key = (b.get("sido") or "").strip()
        row = region_summary.setdefault(
            key, {"key": key, "label": ug_regions.sido_label(key or None),
                  "count": 0, "sigungu": {}})
        row["count"] += 1
        gu = (b.get("sigungu") or "").strip() or "구·군 미지정"
        row["sigungu"][gu] = row["sigungu"].get(gu, 0) + 1
    region_rows = sorted(region_summary.values(),
                         key=lambda r: (-r["count"], r["label"]))
    for r in region_rows:
        r["sigungu"] = sorted(r["sigungu"].items(), key=lambda x: (-x[1], x[0]))

    shown = board
    if region:
        shown = [b for b in shown if (b.get("sido") or "").strip() == region]
        if sigungu:
            shown = [b for b in shown
                     if (b.get("sigungu") or "").strip() == sigungu]
    map_points = board_map.build_points(shown, db, allowed_domains=scope)
    try:
        pending = ug_notify.pending_count(db, scope)
    except Exception:  # noqa: BLE001
        pending = 0

    crowd = _crowd_summary()
    crowd.update(label="인파관리", href="/crowd")
    # 2026-08-21 도메인 분리 — 타일도 둘로 나눈다.
    tr = _traffic_summary()
    tr.update(label="교통위험", href="/traffic")
    fl = _flood_summary()
    fl.update(label="침수", href="/flood")
    road = _road_summary()
    road.update(label="도로 노면 관리", href="/road")
    weather = _weather_card(board, region)
    return templates.TemplateResponse(request, "home.html", {
        **base_ctx(request), "active": "dashboard",
        "now": time.strftime("%Y-%m-%d %H:%M"),
        "domains": [tr, fl, crowd, road],
        "situations": situations,
        "ev_counts": ev_counts,
        "map_points": map_points,
        "pending_notifications": pending,
        # ★ 선행 경고 — 상류에서 사건이 열려 있으면 하류를 미리 알린다.
        # 관계가 등록돼 있지 않으면 빈 목록이라 카드 자체가 안 보인다.
        "flow_warnings": _flow_warnings(db),
        "region_rows": region_rows,
        "region_sel": region,
        "sigungu_sel": sigungu,
        "region_shown": len(shown),
        "region_total": len(board),
        "weather": weather,
        # 배경지도 설정. 비어 있으면 화면은 기존 배치 도식을 그대로 쓴다.
        "map_tile_url": ug_settings.get(ug_settings.KEY_MAP_TILE_URL, db),
        "map_attribution": ug_settings.get(ug_settings.KEY_MAP_ATTRIBUTION, db),
        "map_max_zoom": ug_settings.get(ug_settings.KEY_MAP_MAX_ZOOM, db),
        # 서비스 재기동 카드. 시스템관리자에게만 버튼이 보인다.
        "svc": ug_service.state(watchers=_watcher_count(),
                                prof=routes_server.current_profile(db)),
        "can_restart_service": _can_restart_service(request),
    })


# ---------- 서비스 재기동 (S-01 우측 카드) ----------
#
# 권한을 SETTINGS_SYS × EXECUTE 로 잡았다. 이 조합은 **시스템관리자만** 가진다
# (MGR·OPR 은 SETTINGS_SYS 가 빈 집합). 자원을 새로 만들지 않은 이유는, 재기동이
# 「시스템 설정을 실제로 적용하는 행위」라 기존 자원의 EXECUTE 와 뜻이 맞기
# 때문이다. 관제요원이 실수로 누르는 일은 이 한 줄로 막힌다.
SERVICE_RESTART = "service.restart"


def _can_restart_service(request: Request) -> bool:
    u = getattr(request.state, "user", None)
    if u is None:
        return False
    return UG_ROLES.can(u.role, UG_ROLES.SETTINGS_SYS,
                        UG_ROLES.Action.EXECUTE)


@app.get("/api/service/state")
def api_service_state(db=Depends(get_db),
                      _=Depends(require(UG_ROLES.SETTINGS_SYS,
                                        UG_ROLES.Action.VIEW))):
    """재기동 카드가 읽고, 재기동 뒤 「다시 떴는가」를 확인할 때도 쓴다."""
    return ug_service.state(watchers=_watcher_count(),
                            prof=routes_server.current_profile(db))


@app.post("/api/service/restart")
def api_service_restart(request: Request, db=Depends(get_db),
                        user=Depends(require(UG_ROLES.SETTINGS_SYS,
                                             UG_ROLES.Action.EXECUTE))):
    from ..core import audit as ug_audit

    # 운영체제·감시 방식에 따라 판단이 달라진다(S-87).
    prof = routes_server.current_profile(db)
    ok, why = ug_service.can_restart(prof)
    if not ok:
        return JSONResponse({"ok": False, "error": why}, status_code=409)

    stopping = _watcher_count()
    # **먼저 기록하고 커밋한다.** 죽은 뒤에는 아무것도 남길 수 없다.
    ug_audit.record_and_commit(
        db, action=SERVICE_RESTART, user=user, ip=client_ip(request),
        target="서비스 프로세스",
        after={"uptime_sec": int(ug_service.uptime_seconds()),
               "watchers_stopped": stopping,
               "os": prof["os"], "strategy": prof["strategy"]})

    ug_service.request_restart(_shutdown_workers)
    return {"ok": True, "watchers_stopped": stopping,
            "message": f"재기동합니다. 상시 탐지 {stopping}개가 잠시 멎습니다."}


@app.get("/flood", response_class=HTMLResponse)
@app.get("/flood/runs", response_class=HTMLResponse)
@app.get("/traffic", response_class=HTMLResponse)
@app.get("/crowd", response_class=HTMLResponse)
@app.get("/crowd/cases", response_class=HTMLResponse)
@app.get("/road", response_class=HTMLResponse)
@app.get("/road/detect", response_class=HTMLResponse)
def legacy_dashboard(request: Request):
    """기존 대시보드. v4 IA에서 「도메인별 심층 분석」 위치로 내려왔다.

    경로에 따라 초기 탭만 다르게 열어 준다. 화면 자체를 다시 만들지 않고
    기존 자산을 그대로 쓰되, 제품 셸(메뉴·계정)은 앞으로 이관한다.

    ★ 2026-08-21: ``/traffic`` 을 추가했다. 침수·교통을 나눈 뒤에도 이 경로가
    없으면 **교통만 담당하는 사용자는 상황판에 아예 들어올 수 없다** —
    ``/flood`` 는 가드가 침수 담당만 통과시키기 때문이다.

    ★ 2026-08-24: ``/road``와 ``/road/detect``가 그동안 **같은 탭 하나**를
    열었다 — 메뉴는 「노면 현황(S-40)」·「AI 탐지 실행(S-41)」 둘인데 화면은
    하나뿐이라 사용자가 혼동했다. 이제 ``/road/detect``는 별도 탭
    ``road-detect``를 연다(``index.html``에서 탭 자체를 분리) — 「/road」
    접두사 검사보다 **먼저** 확인해야 한다(그러지 않으면 항상 "road"로
    떨어진다).
    """
    path = request.url.path
    tab = ("runs" if path.endswith("/runs") else
           "traffic" if path.startswith("/traffic") else
           "crowd" if path.startswith("/crowd") else
           "road-detect" if path.startswith("/road/detect") else
           "road" if path.startswith("/road") else "flood")
    # 왼쪽 메뉴에서 지금 보고 있는 항목이 강조되어야 한다. 메뉴가 계속
    # 보이는데 현재 위치를 알 수 없으면 오히려 헷갈린다.
    active = {"flood": "flood-monitor", "runs": "flood-runs",
              "traffic": "traffic-monitor",
              "crowd": "crowd-monitor", "road": "road-status",
              "road-detect": "road-detect"}[tab]
    return templates.TemplateResponse(request, "index.html", {
        # 화면 목록은 「사용」 지정 전부다(상시 + 선택). 상시만 넘기면 관리자가
        # S-80에서 지정한 지점이 화면에서 사라져 설정이 안 먹은 것처럼 보인다.
        **base_ctx(request), "blocks": flood_cameras(), "initial_tab": tab,
        "active": active,
        # 화면 정렬(S-85). 「고정」이면 값이 바뀌어도 카드 자리가 그대로다.
        "card_order": ug_settings.get(ug_settings.KEY_CARD_ORDER),
        "object_order": ug_settings.get(ug_settings.KEY_OBJECT_ORDER),
    })


@app.get("/branding/logo/{name}")
def branding_logo(name: str):
    """기관이 올린 로고. **로그인 화면에도 나와야 해서 공개 경로다**(가드 PUBLIC).

    파일명만 취해 경로 조작을 막고, 실제 저장 위치 밖은 내주지 않는다.
    """
    path = ug_branding.logo_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="로고를 찾을 수 없습니다.")
    return FileResponse(path, media_type=ug_branding.media_type(name),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def api_health():
    import os as _os

    from ..core import ffmpeg_relay as _ffmpeg_relay
    from ..core import video_quality as _video_quality
    return {
        "status": "ok",
        # 2026-09-01 — 서비스 관리(관리자 전용, /admin/services)가 쓴다.
        # `core/service_control.py`의 전역은 프로세스마다(임포트 시점에)
        # 따로 생기므로, 5개 서비스 각자가 자기 자신의 값을 보고한다.
        "pid": _os.getpid(),
        "uptime_sec": round(ug_service.uptime_seconds(), 1),
        "supervised": ug_service.is_supervised(),
        # ⚠️ 2026-08-30 — ffmpeg 릴레이 8개가 밤새 전부 죽어도 12시간
        # 동안 아무도 몰랐던 사고 이후 신설. 가동 대수·재시작 이력을
        # 여기서 바로 확인할 수 있게 한다(core/ffmpeg_relay.py::status()).
        "restream_relay": _ffmpeg_relay.status(),
        # ⚠️ 2026-08-30 — 상시 화질 감시 2단계. 손상 카메라를 "사용자가
        # 우연히 제보"가 아니라 스스로 찾아내게 한다(core/video_quality.py).
        # 이건 4개 도메인 공용 인프라라 여기 그대로 둔다(카메라별로 재는
        # 것이지 특정 도메인 소유가 아니다).
        "video_quality": _video_quality.status(),
        # ⚠️ 2026-08-31 — API 게이트웨이 Phase 1~4(인파·노면·침수·교통위험
        # 서비스 분리)로 "case_count"·"notifications"·"crowd_sources"·
        # "block_count"·"water_segmentation"·"flood_corrupted_skips"·
        # "continuous.*"는 전부 해당 서비스(crowd:8034·road:8035·
        # flood:8036·traffic:8037)의 자체 /api/health로 옮겼다. 이
        # 프로세스는 더 이상 그 어떤 도메인의 실시간 탐지 상태도 모른다
        # (서비스 간 동기 호출로 대신 물어보지 않는다 — 계획서 §서비스
        # 간 통신 원칙) — 이것이 독립 배포의 최종 형태다.
        # ⚠️ 2026-09-01 — 그렇다고 관리자가 "5개 서비스가 지금 실제로
        # 살아 있는가"를 확인할 방법이 아예 없어서는 안 된다. 그 필요는
        # 이 상시 응답(위 원칙이 지키려던 대상)이 아니라 관리자가 직접
        # 연 별도 화면(`/admin/services`, 저빈도·시간제한 호출)이
        # 담당한다 — `routes_services.py` 참고.
    }


# ⚠️ 2026-08-31 — `_crowd_source_status()`는 API 게이트웨이 Phase 1로
# crowd_service.py로 이관됐다(그 프로세스의 자체 /api/health가 담당).


# ── flood/traffic_weather: multi-block live risk (from flood3) ──────────────
@app.get("/api/blocks")
def api_blocks():
    """침수 탐지 대상 목록. **S-80에서 「사용」으로 지정한 전부**를 내려준다.

    ``mode`` 로 상시/선택을 구분한다 — 선택 지점은 상시 분석을 돌지 않아
    실시간 지표가 비어 있으므로, 화면이 그 사실을 표시할 수 있어야 한다.
    """
    return {"blocks": [{"block_id": c["id"], "name": c["name"],
                        "coordinates": c["coordinates"],
                        "mode": c["mode"], "has_roi": c["has_roi"]}
                       for c in flood_cameras()]}


# ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 예전에 여기 있던
# `_rewrite_whep_urls()`·`/api/risk`·`/api/risk/{block_id}`·
# `/api/history`·`/api/stream/risk`는 전부 flood_service.py(포트 8036)·
# traffic_service.py(포트 8037)로 옮겼다(각자 자기 도메인만 담은 새
# 이름 — flood는 `/api/flood-risk*`·`/api/stream/flood-risk`, traffic은
# 기존 이름을 그대로 물려받음). 두 서비스가 각자 자기 `RiskStore`를
# 갖고, whep_url 재계산도 각자 독립적으로 한다(같은 R-01 로직을
# 복제 — `core.restream.resolved_whep_url()`은 순수 함수라 안전하다).


@app.get("/api/record/{block_id}")
def api_record(block_id: str, seconds: int = 10):
    """Record ``seconds`` of the live stream to an mp4 for download (reuses
    the ffmpeg-based cctv_capture helper).

    ⚠️ 2026-08-31(Phase 4) — 예전에는 ``runner.current_blocks()``(인메모리)
    에서 카메라 소스를 찾았다. 침수·교통이 별도 프로세스가 되며 이
    프로세스엔 그 러너가 없다 — DB에서 직접 조회하도록 바꿨다. 순수
    CCTV 녹화 기능이라 애초에 어느 도메인 소유도 아니었으므로(도메인
    무관 조회로 바뀌어도 의미가 그대로다), 이 라우트 자체는 옮기지
    않고 platform-shell에 그대로 둔다.
    """
    from ..core import cameras as _cams
    from ..core.db import get_session as _get_session

    _db = _get_session()
    try:
        cam = _cams.get(_db, block_id)
        if cam is None:
            return {"error": "block not found", "block_id": block_id}
        b = _cams.to_block_dict(cam)
    finally:
        _db.close()
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


@app.get("/api/roi/{block_id}")
def api_roi(block_id: str, domain: str = "flood"):
    """ROI 도형(폴리곤·선·화살표)을 실시간 CCTV 모달의 오버레이용으로 낸다
    (flood3 원본에는 없던 기능, 사용자 요청으로 추가 — docs/integration_plan.md).
    SVG viewBox를 frame_width/frame_height로 맞추면 비디오가 어떤 크기로
    렌더링되든 좌표가 자동으로 맞는다.

    ⚠️ 2026-08-26 — **예전에는 `domain` 인자가 아예 없어 항상 침수(flood)
    도메인만 조회했다.** `openLive()`(app.js)가 침수·교통위험·노면 3개
    도메인의 실시간 모달을 전부 공유하는데, 이 엔드포인트가 그 사실을
    모른 채 침수 전용 `common.roi.load_roi_for_camera(..., "flood", ...)`
    만 불렀다 — 그 결과 교통·노면 카메라의 실제 ROI(`congestion_roi`·
    `analysis_roi` 등)는 절대 조회되지 않고, 그 카메라에 우연히 침수
    ROI도 없으면 "ROI가 설정되어 있지 않습니다"만 떴다(강서구청/
    SEOUL-207의 노면 실시간 관제에서 실측 확인 — 노면 `analysis_roi`는
    DB에 있는데도 안 보였다).

    **침수(flood) 도메인은 기존 DB→레거시 파일 폴백을 그대로 보존한다**
    (`common.roi.load_roi_for_camera`) — 파일이 남아 있는 배포를 갑자기
    깨뜨리지 않기 위해서다. 다른 도메인은 ROI 편집기가 이미 쓰는 범용
    함수 `core.cameras.roi_of()`를 그대로 재사용한다(같은 조회 로직을
    또 만들지 않는다) — 단, 이쪽은 레거시 파일 폴백이 없다(애초에 그런
    파일이 존재한 적이 없는 도메인들이라 폴백 대상 자체가 없다).

    응답 형태를 도메인 공통으로 통일했다 — 예전에는 `road_roi`·
    `low_point_roi`·`lane_threshold_line`이 최상위 키였지만(침수 전용
    이름), 이제 `shapes`(도형 키→좌표) + `shape_kinds`(도형 키→
    polygon/line/arrows)로 낸다. 프런트가 `core.cameras.ROI_SHAPES`와
    같은 구조를 그대로 받아, 어떤 도메인이 오든 하드코딩 없이 그릴 수
    있다(ROI 편집기 `camera_roi.html`의 방식과 동일)."""
    from ..core import cameras as C

    if domain not in C.ROI_SHAPES:
        return {"error": f"알 수 없는 도메인입니다: {domain}", "block_id": block_id}

    if domain == "flood":
        from ..common.roi import load_roi_for_camera
        path = PROJECT_ROOT / "configs" / "roi" / f"{block_id}.json"
        cfg = load_roi_for_camera(block_id, "flood", fallback_path=path)
        if not (cfg.has_road or cfg.has_low_point or cfg.has_lane_line):
            return {"error": "no ROI configured for this block", "block_id": block_id,
                    "domain": domain}
        shapes = {"road_roi": cfg.road_roi or [], "low_point_roi": cfg.low_point_roi or [],
                  "lane_threshold_line": cfg.lane_threshold_line or []}
        frame_w, frame_h = cfg.frame_width, cfg.frame_height
    else:
        from ..core.db import get_session
        db = get_session()
        try:
            roi = C.roi_of(db, block_id, domain)
        finally:
            db.close()
        shapes = roi.get("shapes") or {}
        frame_w, frame_h = roi.get("frame_width"), roi.get("frame_height")
        if not any(shapes.get(key) for key, *_rest in C.ROI_SHAPES[domain]):
            return {"error": "no ROI configured for this block", "block_id": block_id,
                    "domain": domain}

    return {
        "block_id": block_id, "domain": domain,
        "frame_width": frame_w, "frame_height": frame_h,
        "shapes": shapes,
        "shape_kinds": {key: kind for key, _label, kind, _req in C.ROI_SHAPES[domain]},
        "shape_labels": {key: label for key, label, _kind, _req in C.ROI_SHAPES[domain]},
    }


# ⚠️ 2026-08-31(Phase 4) — `/api/report/{block_id}`는 traffic_service.py로
# 옮겼다. 이 보고서가 읽는 필드(risk_name/level/severity/mean_speed/
# speed_drop/state/queue_len/stalled)가 전부 교통 전용이라 처음부터
# 이 도메인 소유였다(main.py는 더 이상 `store`를 안 가진다).


def _restream_whep_url_for(camera_id: str, db,
                           request_host: str | None = None) -> str | None:
    """관제요원 브라우저가 WHEP으로 재생할 주소. 재배포가 꺼져 있거나
    MediaMTX가 응답하지 않으면 ``None`` — `app.js::openLive()`가 그 경우
    기존 hls.js 경로로 자연히 떨어진다.

    ``service/runner.py::_restream_whep_url()``과 같은 판단이지만, 이미
    열려 있는 요청 처리용 세션(``db``)을 그대로 받는다는 점만 다르다
    (runner.py 쪽은 파이프라인 워커 스레드라 요청 세션이 없어 직접 연다).

    ⚠️ 2026-08-29(R-01) — 실제 판단 로직은 `core/restream.py::
    resolved_whep_url()`로 옮겼다(runner.py 쪽과 중복 구현이던 것을
    합침 — 같은 버그가 두 곳에서 따로 발견·수정된 전례가 있어서다).
    ``request_host``는 요청 처리 스레드라 접속 호스트를 알 수 있는
    호출부(도로·인파 API)가 넘긴다 — 접속자와 무관하게 항상 127.0.0.1로
    조립되던 문제(공개 호스트 미설정 시)를 여기서 고친다.
    """
    from ..core import restream as _restream
    return _restream.resolved_whep_url(camera_id, db, request_host=request_host)


# ── road: 도로 노면 관리 ────────────────────────────────────────────────────
# 대상은 **S-80에서 「노면 사용」으로 지정한 CCTV**다. 예전에는 침수 카메라
# 목록(BLOCKS)에 모의값을 얹어 보여 줬는데, 관리자가 지정한 것과 화면이 달라
# 설정이 반영되지 않은 것처럼 보였다.
def road_cameras(request_host: str | None = None) -> list[dict]:
    """노면 탐지 대상 카메라. DB를 읽지 못하면 빈 목록.

    ``request_host`` — R-01(2026-08-29). WHEP 주소가 접속자와 무관하게
    항상 127.0.0.1로 조립되던 문제를 고치려면, whep_url을 실제로
    화면에 실어 보내는 호출부(``api_road_live()``)가 이 요청의 접속
    호스트를 넘겨야 한다. 넘기지 않는 다른 호출부(요약 타일 등, whep_url
    미노출)는 기존처럼 동작한다.
    """
    try:
        from ..core import cameras as _cams
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
                    # 실시간 관제(S-44)가 화면에 영상을 띄우려면 스트림 주소가
                    # 필요하다. 업로드 동영상(video)은 로컬 경로라 브라우저가
                    # 열 수 없으므로 내려주지 않는다.
                    "stream_url": (c.source_url or "") if c.source_type == "hls" else "",
                    # ★ 2026-08-28 — CCTV 재배포 허브. 재배포가 꺼져 있거나
                    # 응답이 없으면 None — app.js::openLive()가 위 hls.js
                    # 경로로 자연히 떨어진다.
                    "whep_url": (_restream_whep_url_for(c.id, db, request_host=request_host)
                                if c.source_type == "hls" else None),
                    # ⚠️ 2026-08-30 — 상시 화질 감시. warn/crit이면 화면이
                    # 실시간 영상을 열기 전에도 미리 배지로 알려준다.
                    "quality_grade": (_video_quality.grade_for(c.id)
                                      if c.source_type == "hls" else None),
                    "mode": "continuous" if (row and row.continuous) else "selective",
                })
            return out
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[road] 노면 카메라 목록 조회 실패: {str(e)[:120]}")
        return []


# ⚠️ 2026-08-31 — API 게이트웨이 Phase 2: 노면관리(`/api/road/*`·
# `/api/road-analysis/*`·`/api/road-report/*`)가 별도 서비스
# (service/road_service.py, 포트 8035)로 이관됐다. `/road`·`/road/detect`
# 화면(HTML)만 여전히 이 프로세스가 서빙한다(Phase 1의 `/crowd`와 같은
# 이유 — 공용 셸). `road_cameras()`(위)는 홈 화면 요약(`_road_summary()`)
# 이 아직 써서 이 프로세스에도 남겨 둔다 — road_service.py에도 같은
# 로직을 복제해 둔다(계획서 §core/common 처리: 핫패스 헬퍼는 네트워크
# 호출 대신 공유 라이브러리처럼 복제하는 편이 더 단순하다).


# ⚠️ 2026-08-31 — API 게이트웨이 Phase 1: 인파관리(SAM3 케이스 뷰어 +
# 알림, /api/cases*·/api/crowd/*·/api/notifications/*·
# /media/{case_id}/*)가 별도 서비스(service/crowd_service.py, 포트
# 8034)로 이관됐다. `/crowd`·`/crowd/cases` 화면(HTML)만 여전히 이
# 프로세스가 서빙한다(4개 도메인 공용 셸이라 화면까지 옮기면 좌측
# 메뉴·계정 UI를 전부 복제해야 해서 이득보다 비용이 크다 — 계획서
# §서비스 경계 지도). 게이트웨이(nginx)가 `/api/crowd/*`·`/api/cases*`·
# `/media/{case_id}/*` 경로만 crowd-service로 보낸다.


@app.get("/media/flood-runs/{run_id}/{asset_path:path}")
def api_flood_run_media(run_id: str, asset_path: str):
    run_dir = _resolve_run_dir(run_id)
    file_path = (run_dir / asset_path).resolve()
    if file_path != run_dir and run_dir not in file_path.parents:
        raise HTTPException(status_code=404, detail="미디어 파일을 찾을 수 없습니다.")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="미디어 파일을 찾을 수 없습니다.")
    return FileResponse(file_path, filename=None)


# ── flood standalone-pipeline runs (tot-flood-standalone --video ...) ───────
# Archived by common.case_archive.run_writer.RunWriter (annotated frames/video
# already have the ROI/water/detection overlay drawn in -- see
# flood.visualization.annotate_combined) so a CLI analysis run shows up here
# without any extra step.
_RUN_ID_RE = re.compile(r"^run_[0-9]{8}_[0-9]{6}$")


def _resolve_run_dir(run_id: str) -> Path:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="분석 결과를 찾을 수 없습니다.")
    run_dir = (RUNS_DIR / run_id).resolve()
    if run_dir.parent != RUNS_DIR.resolve() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="분석 결과를 찾을 수 없습니다.")
    return run_dir


@app.get("/api/flood-runs")
def api_list_flood_runs():
    runs = list_runs()
    return {
        "runs": [
            {
                "run_id": r.run_id,
                "frames": r.frames,
                "max_alert_level": r.max_alert_level,
                "source_name": r.source_name,
                "has_video": r.has_video,
            }
            for r in runs
        ]
    }


def _records_json_safe(df):
    """metrics.csv/alert_log.csv leave some numeric columns blank (e.g.
    avg_vehicle_pixel_speed when tracking is off); pandas reads those back as
    NaN, which plain json.dumps (used by FastAPI's default response class)
    rejects with ValueError. Swap NaN -> None so it serializes as JSON null."""
    if df.empty:
        return []
    return df.astype(object).where(df.notna(), None).to_dict(orient="records")


@app.get("/api/flood-runs/{run_id}")
def api_flood_run_detail(run_id: str):
    run_dir = _resolve_run_dir(run_id)
    data = load_run(run_dir)
    return {
        "run_id": run_id,
        "config": data["config"],
        "video_url": f"/media/flood-runs/{run_id}/processed_video.mp4" if data["video"] else None,
        "snapshot_urls": [f"/media/flood-runs/{run_id}/snapshots/{p.name}" for p in data["snapshots"]],
        "frame_urls": [f"/media/flood-runs/{run_id}/annotated_frames/{p.name}" for p in data["annotated_frames"]],
        "metrics": _records_json_safe(data["metrics"]),
        "alerts": _records_json_safe(data["alerts"]),
    }


def run() -> None:
    """Console-script entry point (``tot-service``)."""
    import uvicorn
    uvicorn.run("tot_dashboard.service.main:app", host="127.0.0.1", port=8000)
