"""서비스 관리 — 5개 서비스(플랫폼-쉘·인파관리·노면관리·침수·교통위험)의
현재 가동 정보·상태를 한 화면에서 확인한다 (관리자 전용, 2026-09-01
사용자 요청으로 신설 — 아직 UI 설계서에 화면번호 미배정. 「위험도
임계값(교통)」·「위험등급 관리」 등 기존 화면도 같은 사유로 화면번호
없이 먼저 만들어졌다).

## 왜 필요한가

2026-08-31 API 게이트웨이 도입(Phase 1~4)으로 4개 도메인이 각자 독립된
프로세스(포트 8034~8037)로 떨어져 나갔다. 그런데 "지금 5개 프로세스가
전부 살아 있는가"를 한눈에 보여주는 화면이 그동안 하나도 없었다 — 각
서비스의 ``/api/health``를 포트 번호까지 직접 알아야만(또는 이 세션이
그랬듯 CPU 부하로 하나가 죽어야 뒤늦게) 확인할 수 있었다.

## 왜 서비스 간 동기 호출을 여기서는 써도 되는가

``service/main.py::api_health()`` 의 주석대로, 상시 홈 화면(초 단위로
반복 폴링/SSE)에서는 서비스 간 동기 HTTP 호출을 쓰지 않기로 이미
결정했다(계획서 §서비스 간 통신 원칙) — 죽은 서비스 하나가 홈 화면
전체를 함께 느리게 만들면 안 되기 때문이다. **이 화면은 그 원칙이
막던 상황과 성격이 다르다**:

* **관리자가 필요할 때만 연다** — 상시 폴링 대상이 아니다.
* **목적 자체가 "지금 이 순간 응답하는가 확인"**이다 — DB의 마지막
  관측 시각으로는 알 수 없다(카메라 관측이 끊겨도 프로세스는 살아
  있을 수 있고, 그 반대도 있다 — 이번 세션에 직접 겪은 그대로다).
* 서비스별 호출에 **짧은 시간제한**(2초)을 걸고 **병렬로** 확인해,
  죽은 서비스 하나가 이 화면 하나를 조금 늦출지언정(최악 2초) 다른
  무엇도 막지 않는다.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..common.config import PROJECT_ROOT
from ..core import audit
from ..core import roles as R
from ..core import server_profile as SP
from ..core import service_control as ug_service
from ..core import settings
from ..core.auth import client_ip, get_db, require, require_page
from ..core.models import User

HEALTH_TIMEOUT_SEC = 2.0

SCRIPTS_DIR = PROJECT_ROOT / "scripts"
OPS_LOG = PROJECT_ROOT / "data" / "logs" / "admin-service-ops-console.log"

# 2026-09-01 신설 — 관리자가 정지/시작할 수 있는 서비스. platform-shell
# (main) 은 뺀다 — 자기 자신을 죽이고 감시자가 되살리는 별도 방식(홈
# 화면 "서비스 재기동")이 이미 있고, 그 방식은 이 정지/시작(자기
# 자신이 아닌 다른 프로세스를 다루는 것)과 성격이 다르다.
STOPPABLE = ("crowd", "road", "flood", "traffic")

# core/audit.py 에 전역 등록하지 않는다 — service/main.py 의
# `SERVICE_RESTART = "service.restart"` 가 이미 같은 방식으로 그 파일
# 안에 로컬로 두는 전례다.
SERVICE_STOP = "service.stop"
SERVICE_START = "service.start"

# (key, 표시 이름, 포트, 이 프로세스 자기 자신인가)
SERVICES: tuple[tuple[str, str, int, bool], ...] = (
    ("main", "플랫폼-쉘(통합관제·설정)", 8033, True),
    ("crowd", "인파관리", 8034, False),
    ("road", "노면관리", 8035, False),
    ("flood", "침수", 8036, False),
    ("traffic", "교통위험", 8037, False),
)


def _check_local() -> dict:
    """이 프로세스(platform-shell) 자기 자신 — 네트워크를 타지 않는다."""
    return {
        "ok": True,
        "latency_ms": 0.0,
        "detail": {
            "pid": os.getpid(),
            "uptime_sec": round(ug_service.uptime_seconds(), 1),
            "supervised": ug_service.is_supervised(),
        },
    }



# ⚠️ 실기 확인(2026-09-01, py-spy 라이브 스택으로 직접 포착) — 처음에는
# 매 확인마다 `httpx.get(...)`(모듈 수준 편의 함수)를 그대로 불렀다.
# 이 함수는 호출할 때마다 **새 `httpx.Client`를 만들고 버린다** — 내부적으로
# `ssl.create_default_context()`(윈도우에서 OS 인증서 저장소를 읽는,
# 실측상 결코 가볍지 않은 호출)를 매번 새로 탄다. `http://` 요청이라
# TLS를 안 쓰는데도 기본 전송 계층이 SSL 컨텍스트를 무조건 만든다.
# 이 화면을 여러 번 열거나(관리자가 새로고침을 반복하거나, 이 세션이
# 진단 중 그랬듯 요청이 겹치면) `_collect()` 호출마다 4개씩 새
# 컨텍스트가 쌓여, CPU 100% 아래에서 서로 GIL을 다투며 응답시간이
# 수십~수백 초까지 늘어나는 것을 직접 관측했다. 프로세스 하나에서
# 재사용하는 `httpx.Client` 하나로 바꿔 이 문제를 없앤다.
_HTTP_CLIENT: httpx.Client | None = None


def _http_client() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.Client()
    return _HTTP_CLIENT


def _check_remote(port: int) -> dict:
    t0 = time.monotonic()
    try:
        r = _http_client().get(f"http://127.0.0.1:{port}/api/health",
                               timeout=HEALTH_TIMEOUT_SEC)
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code != 200:
            return {"ok": False, "latency_ms": latency_ms,
                    "detail": {"status_code": r.status_code}}
        return {"ok": True, "latency_ms": latency_ms, "detail": r.json()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "detail": {"error": str(e)[:200]}}


def _collect(db: Session | None = None) -> list[dict]:
    """5개 서비스를 병렬로 확인한다 — 순차로 하면 전부 죽었을 때 최악의
    경우 5×2초(10초)를 기다린다. 병렬이면 가장 느린 것 하나(최대 2초)만
    기다리면 된다."""
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as ex:
        futures = {
            key: (ex.submit(_check_local) if is_self
                  else ex.submit(_check_remote, port))
            for key, _label, port, is_self in SERVICES
        }
        results = {k: f.result() for k, f in futures.items()}

    # 2026-09-02 신설 — "응답 없음"과 "관리자가 정지함"을 구분한다.
    # 구분 없이 전부 "응답 없음"으로 뭉치면 관제요원이 사고인지 의도인지
    # 헷갈린다(이 파일 머리말의 "정직한 상태 표시" 원칙과 같은 이유).
    admin_stopped = settings.admin_stopped_services(db)

    out = []
    for key, label, port, _is_self in SERVICES:
        r = results[key]
        detail = r["detail"]
        # 화면이 pid·uptime_sec·supervised는 전용 칸에 따로 그린다 —
        # "세부 정보" 칸에는 그 나머지(서비스마다 다른 도메인 고유 값)만
        # 남긴다. 템플릿의 Jinja 필터 체인보다 여기서 거르는 편이
        # 읽기·시험 모두 쉽다.
        extra = {k: v for k, v in detail.items()
                if k not in ("pid", "uptime_sec", "supervised")}
        out.append({"key": key, "label": label, "port": port, **r,
                   "extra": extra, "admin_stopped": key in admin_stopped})
    return out


def _launch_script(name: str) -> subprocess.Popen:
    """윈도우 배포 스크립트(`scripts/ensure-*.ps1`·`scripts/stop-*.ps1`)를
    fire-and-forget으로 실행한다 — 완료를 기다리지 않는다(호출자가
    돌려받은 `Popen`을 원하면 별도로 기다릴 수 있다 — 아래
    `_try_start_operation` 참고).

    ⚠️ 왜 새 정지 메커니즘(예: 대상 서비스에 "스스로 멈춰라" HTTP 요청)을
    안 만들고 이미 있는 이 스크립트를 그대로 쓰는가 — 오늘 CPU 100%
    아래에서 사소한 요청조차 10~170초 걸리는 것을 실측했다. **정지시키고
    싶은 바로 그 순간** 그 서비스가 응답을 못 하고 있을 가능성이 높은데,
    HTTP로 "네 스스로 멈춰라"라고 부탁하는 방식은 정확히 그 순간 못
    쓰게 된다. `stop-*.ps1`은 명령줄 문자열 매칭으로 OS가 직접
    프로세스를 찾아 죽이므로 대상의 응답성과 무관하게 동작한다
    (2026-09-01 계획 §사전 조사로 확인한 사실 참고).

    ``ensure-*.ps1``(시작)은 flood/traffic이 카메라별 YOLO 모델을
    모듈 임포트 시점에 동기로 로드해 최대 130초 걸리므로, 호출자도
    완료를 기다리지 말고 곧바로 돌아가야 한다 — 화면이 대신
    `/api/admin/services/state`를 폴링한다.
    """
    OPS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(OPS_LOG, "ab") as log_f:
        return subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(SCRIPTS_DIR / name)],
            cwd=str(PROJECT_ROOT), stdout=log_f, stderr=log_f,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


# ⚠️ 실기 확인(2026-09-02) — API를 화면 버튼이 아니라 직접(curl 등으로)
# 재시도하면 같은 키에 대해 정지/시작이 중복 실행될 수 있음을 실제로
# 겪었다(클라이언트 타임아웃으로 재시도했는데 서버는 이미 성공해
# `ensure-crowd-service.ps1`가 두 번 동시에 떴다). 화면의 버튼 비활성화는
# 이 경로(API 직접 호출)를 못 막는다 — 서버 쪽 잠금이 필요하다.
#
# 타임아웃을 새로 지어내는 대신, 이미 띄운 PowerShell 프로세스가 **실제로
# 끝나는 순간**(그 스크립트 자신이 이미 "포트가 열릴/닫힐 때까지
# 기다렸다 끝난다"는 계약을 지키고 있다)을 감시해 그때 잠금을 푼다.
_OPS_LOCK = threading.Lock()
_IN_PROGRESS: set[str] = set()  # 지금 정지/시작이 진행 중인 서비스 키
_OP_WAIT_TIMEOUT_SEC = 300  # 방치된 프로세스에 대한 안전판(시작 최대
                           # 실측치 90~130초보다 넉넉히 여유를 둔다)


def _try_start_operation(key: str, script_name: str) -> bool:
    """이 키에 이미 진행 중인 정지/시작이 있으면 잠그지 않고 False를
    돌려준다(호출자는 409로 응답한다). 아니면 잠그고 스크립트를 띄운
    뒤, 그 프로세스가 실제로 끝나는 순간(또는 안전판 시간 초과) 잠금을
    스스로 풀도록 감시 스레드를 하나 띄우고 True를 돌려준다.

    정지·시작을 구분하지 않고 "이 키에 지금 진행 중인 조작이 있는가"
    하나만 본다 — 정지가 끝나기 전에 같은 키로 시작이 끼어드는 것도
    막아야 하고, 그 반대도 마찬가지이기 때문이다.
    """
    with _OPS_LOCK:
        if key in _IN_PROGRESS:
            return False
        _IN_PROGRESS.add(key)
    try:
        proc = _launch_script(script_name)
    except Exception:
        with _OPS_LOCK:
            _IN_PROGRESS.discard(key)
        raise

    def _release_when_done() -> None:
        if proc is not None:
            try:
                proc.wait(timeout=_OP_WAIT_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                pass
        with _OPS_LOCK:
            _IN_PROGRESS.discard(key)

    threading.Thread(target=_release_when_done, daemon=True).start()
    return True


def register(app, templates, base_ctx) -> None:

    @app.get("/admin/services", response_class=HTMLResponse)
    def admin_services(request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_page(R.SETTINGS_SYS,
                                                         R.Action.VIEW))):
        services = _collect(db)
        return templates.TemplateResponse(request, "admin_services.html", {
            **base_ctx(request, user), "active": "admin-services",
            "services": services,
            "ok_count": sum(1 for s in services if s["ok"]),
            "total_count": len(services),
            "is_windows": SP.detect_os() == SP.OS_WINDOWS,
            "stoppable": STOPPABLE,
        })

    @app.get("/api/admin/services/state")
    def api_admin_services_state(
            db: Session = Depends(get_db),
            user: User = Depends(require(R.SETTINGS_SYS, R.Action.VIEW))):
        """화면의 폴링(정지/시작 요청 뒤 상태 변화 확인)이 쓴다 —
        전체 HTML을 반복 재요청하지 않도록 `_collect()`를 그대로
        JSON으로 내려준다."""
        return JSONResponse({"services": _collect(db)})

    @app.post("/admin/services/{key}/stop")
    def admin_service_stop(
            key: str, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require(R.SETTINGS_SYS, R.Action.EXECUTE))):
        if key not in STOPPABLE:
            return JSONResponse(
                {"ok": False, "error": f"정지할 수 없는 서비스입니다: {key}"},
                status_code=400)
        label = next(label for k, label, _p, _s in SERVICES if k == key)
        # 2026-09-02 신설 — 스크립트를 띄우기 **전에** 먼저 "정지 의도"를
        # 기록한다. 순서가 중요하다 — 정지 스크립트가 도는 동안 다른
        # 경로(예: urbanguard-service.ps1 -Action restart의 전체 확인
        # 단계)로 ensure-*.ps1이 걸려도 이미 최신 의도를 보게 하기
        # 위해서다(실사용 중 자체 발견한 사고 — 정지시킨 서비스가
        # 재기동 때 도로 켜졌었다).
        admin_stopped = settings.admin_stopped_services(db)
        settings.set_admin_stopped_services(db, admin_stopped | {key})
        db.commit()
        if not _try_start_operation(key, f"stop-{key}-service.ps1"):
            return JSONResponse(
                {"ok": False,
                 "error": f"{label} 서비스에 대한 작업이 이미 진행 "
                          "중입니다. 잠시 후 다시 시도해 주세요."},
                status_code=409)
        audit.record_and_commit(
            db, action=SERVICE_STOP, user=user, ip=client_ip(request),
            target=key, after={"label": label})
        return {"ok": True,
               "message": f"{label} 서비스 중지를 요청했습니다. "
                          "잠시 후 상태가 갱신됩니다. 관리자가 다시 "
                          "'시작'을 누르기 전까지는 재기동해도 다시 "
                          "켜지지 않습니다."}

    @app.post("/admin/services/{key}/start")
    def admin_service_start(
            key: str, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require(R.SETTINGS_SYS, R.Action.EXECUTE))):
        if key not in STOPPABLE:
            return JSONResponse(
                {"ok": False, "error": f"시작할 수 없는 서비스입니다: {key}"},
                status_code=400)
        if SP.detect_os() != SP.OS_WINDOWS:
            # ⚠️ 리눅스용 시작 스크립트/systemd 유닛이 아직 없다(2026-09-01
            # 계획 §사전 조사로 확인한 사실). 없는 것을 있는 척하지 않는다.
            return JSONResponse(
                {"ok": False,
                 "error": "리눅스에서는 아직 지원되지 않습니다 — 확인이 "
                          "필요합니다(scripts/ensure-*.ps1이 윈도우 전용)."},
                status_code=409)
        label = next(label for k, label, _p, _s in SERVICES if k == key)
        # 2026-09-02 신설 — 위 admin_service_stop과 대칭. ensure-*.ps1을
        # 띄우기 전에 먼저 "정지 의도"를 지운다 — 그래야 그 스크립트가
        # 자기 자신을 "관리자가 정지시킴"으로 오인해 곧바로 SKIPPED로
        # 끝내 버리지 않는다.
        admin_stopped = settings.admin_stopped_services(db)
        settings.set_admin_stopped_services(db, admin_stopped - {key})
        db.commit()
        if not _try_start_operation(key, f"ensure-{key}-service.ps1"):
            return JSONResponse(
                {"ok": False,
                 "error": f"{label} 서비스에 대한 작업이 이미 진행 "
                          "중입니다. 잠시 후 다시 시도해 주세요."},
                status_code=409)
        audit.record_and_commit(
            db, action=SERVICE_START, user=user, ip=client_ip(request),
            target=key, after={"label": label})
        return {"ok": True,
               "message": f"{label} 서비스 시작을 요청했습니다. "
                          "모델을 불러오는 동안 최대 2분 정도 걸릴 수 "
                          "있습니다."}
