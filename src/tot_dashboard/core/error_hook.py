"""백그라운드 오류 수집 — 로깅 → 오류 이력 (S-92).

왜 필요한가
    화면·API 오류는 예외 핸들러로 잡을 수 있지만, **상시 탐지 워처와 분석
    파이프라인은 요청 밖에서 돕니다.** 그쪽에서 난 오류는 콘솔 로그로만 흘러가
    아무도 보지 않습니다. 실제로 밤사이 서비스가 멈춰 있었는데 아침까지 아무도
    몰랐던 일이 있었습니다. 그 경로를 화면으로 끌어오는 것이 이 모듈입니다.

방식
    표준 ``logging`` 에 핸들러를 하나 붙여 ERROR 이상만 골라 기록합니다.
    코드를 여기저기 고쳐 기록 호출을 심는 대신 로깅을 재료로 쓰는 이유는,
    **이미 로그를 남기고 있는 자리**가 곧 오류가 나는 자리이기 때문입니다.

조심한 것
    * 우리 자신의 로거(``urbanguard.errors``)는 제외합니다 — 기록 실패를 기록하려다
      무한히 돕니다. :func:`~.errors.record` 의 재진입 방지와 두 겹입니다.
    * SQLAlchemy·uvicorn 접근 로그처럼 시끄러운 로거는 제외합니다.
    * 핸들러 안에서 난 예외는 삼킵니다. 로깅이 서비스를 죽이면 안 됩니다.
"""
from __future__ import annotations

import logging
import traceback

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from . import errors

# 이 로거들이 낸 기록은 오류 이력으로 올리지 않는다.
EXCLUDED_PREFIXES = (
    "urbanguard.errors",   # 기록 실패 → 다시 기록 → 무한 반복
    "urbanguard.audit",
    "sqlalchemy",          # 엔진 경고까지 다 올리면 목록이 못 쓰게 된다
    "uvicorn.access",
    "watchfiles",
    "multipart",
    "PIL",
    "matplotlib",
)

# 로거 이름 앞부분 → 발생 위치. 화면에서 「어디서 났나」를 가르는 값이다.
_SOURCE_BY_LOGGER = (
    ("urbanguard.road", "worker"),
    ("urbanguard.crowd", "worker"),
    ("urbanguard.flood", "worker"),
    ("urbanguard.continuous", "worker"),
    ("urbanguard.watcher", "worker"),
    ("tot_dashboard.road", "worker"),
    ("tot_dashboard.crowd", "worker"),
    ("tot_dashboard.flood", "worker"),
    ("uvicorn", "web"),
    ("fastapi", "web"),
)


def _source_of(logger_name: str) -> str:
    for prefix, src in _SOURCE_BY_LOGGER:
        if logger_name.startswith(prefix):
            return src
    return "pipeline"


class ErrorLogHandler(logging.Handler):
    """ERROR 이상 로그를 오류 이력으로 옮긴다."""

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            if record.levelno < logging.ERROR:
                return
            name = record.name or ""
            if name.startswith(EXCLUDED_PREFIXES):
                return

            exc = None
            detail = ""
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                detail = "".join(traceback.format_exception(*record.exc_info))

            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001
                # 포매팅 인자가 안 맞는 로그 호출. 원문이라도 남긴다.
                message = str(record.msg)

            errors.record(
                message=message, exc=exc, detail=detail,
                source=_source_of(name),
                # 어느 파일 몇 번째 줄인지가 경로 자리에 들어간다. 백그라운드
                # 오류에는 URL이 없으므로 이 값이 대신 위치를 알려 준다.
                path=f"{record.module}:{record.lineno}",
                severity="critical" if record.levelno >= logging.CRITICAL else "error",
            )
        except Exception:  # noqa: BLE001
            # 로깅 핸들러의 예외는 절대 밖으로 내보내지 않는다.
            self.handleError(record)


_installed: ErrorLogHandler | None = None


def install(level: int = logging.ERROR) -> ErrorLogHandler:
    """루트 로거에 한 번만 붙인다. 이미 붙어 있으면 그대로 돌려준다."""
    global _installed
    if _installed is not None:
        return _installed
    h = ErrorLogHandler(level=level)
    logging.getLogger().addHandler(h)
    _installed = h
    return h


def uninstall() -> None:
    """테스트가 원상복구할 때 쓴다."""
    global _installed
    if _installed is not None:
        logging.getLogger().removeHandler(_installed)
        _installed = None


class ErrorCaptureMiddleware(BaseHTTPMiddleware):
    """화면·API 요청에서 난 오류를 기록한다.

    미들웨어로 잡는 이유
        ``@app.exception_handler(HTTPException)`` 을 쓰면 FastAPI 의 기본 응답
        형식을 우리가 다시 구현해야 하고, 한 군데라도 다르면 화면이 깨집니다.
        미들웨어는 응답을 **바꾸지 않고 지켜보기만** 하므로 그 위험이 없습니다.

    무엇을 남기는가
        * 처리되지 않은 예외 — 스택트레이스까지. 기록한 뒤 **그대로 다시
          던집니다.** 삼키면 사용자가 빈 화면을 보게 됩니다.
        * 5xx 응답 — 예외 없이 코드가 직접 500을 돌려준 경우.

        4xx 는 남기지 않습니다. 잘못 입력한 검색어, 없는 페이지 요청까지 쌓이면
        정작 봐야 할 고장이 묻힙니다. 다만 **권한 규칙 미등록 차단(403)** 은
        설정 결함이라 :mod:`.guard` 가 따로 기록합니다.
    """

    async def dispatch(self, request, call_next):
        # ⚠️ 실기 확인(2026-09-01) — 아래 두 `errors.record()` 호출이 원래
        # 이 async dispatch() 안에서 그대로 동기 호출됐다. `core/guard.py`의
        # `AuthGuard`와 같은 결함: 이벤트루프 한 개짜리 프로세스에서 미들웨어
        # 안 동기 DB 호출은 그 호출이 끝날 때까지 서버 전체를 멈춘다.
        # 하필 **오류가 났을 때·5xx를 낼 때마다** 도는 경로라, 원인 모를
        # 오류가 하나 나면 그 오류를 "기록하는 동안" 서버 전체가 함께
        # 멈추는 최악의 조합이었다(py-spy로 이 파일 안에서 멈춘 것은
        # 아니지만 `guard.py`에서 같은 유형으로 직접 포착 — 같은 저자가
        # 같은 방식으로 반복한 결함이라 함께 고친다). `run_in_threadpool`로
        # 이벤트루프 밖에서 돌린다.
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            await run_in_threadpool(
                errors.record,
                exc=exc, detail=traceback.format_exc(), source="web",
                path=request.url.path, method=request.method, status_code=500,
                login_id=getattr(getattr(request.state, "user", None),
                                 "login_id", "") or "",
                ip=(request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                    or getattr(request.client, "host", "") or ""),
                severity="critical",
            )
            raise
        if response.status_code >= 500:
            await run_in_threadpool(
                errors.record,
                message=f"HTTP {response.status_code} 응답",
                source="web", path=request.url.path, method=request.method,
                status_code=response.status_code, severity="error",
                login_id=getattr(getattr(request.state, "user", None),
                                 "login_id", "") or "",
            )
        return response
