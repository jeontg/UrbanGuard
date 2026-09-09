"""전역 인증·권한 가드 (미들웨어).

엔드포인트마다 Depends를 붙이지 않고 경로 규칙표로 한곳에서 판정한다.
지금 문제가 된 「무인증 알림 발송」이 바로 **가드를 빠뜨려서** 생긴 것이므로,
개별 부착 방식은 같은 사고를 반복한다.

원칙은 **기본 거부(fail-closed)** 다. 규칙표에 없는 /api·/media 경로는 통과시키지
않고 403을 준다. 새 API를 만들면서 규칙 등록을 잊으면 동작하지 않으므로,
"보안이 빠진 채 조용히 열려 있는" 상태가 생기지 않는다.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from . import roles as R
from .db import get_session
from .models import User
from .security import COOKIE_NAME, read_session

log = logging.getLogger("urbanguard.guard")

# 인증 없이 열어 두는 경로. 최소한으로 유지한다.
PUBLIC = (
    re.compile(r"^/login/?$"),
    re.compile(r"^/logout/?$"),
    re.compile(r"^/static/"),
    # 기관 로고. 로그인 화면(미인증 상태)에 표시돼야 해서 공개다.
    # 기관 로고는 대외 공개물이라 노출에 문제가 없다.
    re.compile(r"^/branding/logo/"),
    re.compile(r"^/favicon\.ico$"),
    re.compile(r"^/api/health/?$"),   # 로드밸런서·감시용
    # 2026-09-01 신설 — 로그인 화면의 셀프서비스 가입 신청(GET 폼·POST
    # 접수 둘 다 이 경로 하나). 승인은 관리자 화면(`/admin/users`,
    # 인증 필요)에서만 하므로, 여기 공개하는 것은 "신청 접수" 자체뿐이다.
    re.compile(r"^/signup/?$"),
)

# ★ 2026-09-01 실사용자 제보 — "CCTV 관리엔 상시로 잡히는데 4개 탐지
#   화면엔 아무것도 없다"는 것이 알고 보니 게이트웨이(8080)가 아니라 이
#   프로세스(platform-shell, 보통 8033)로 **직접** 접속해서 생긴 문제였다.
#   여기 적힌 경로들은 `configs/nginx_base.conf`가 이 프로세스가 아니라
#   침수/교통위험/인파관리/노면관리 4개 분리 서비스로 돌려보내는
#   경로다 — 즉 게이트웨이를 거쳤다면 애초에 이 프로세스까지 올 수가
#   없는 경로들이다. 그런데도 여기까지 왔다는 것 자체가 "게이트웨이를
#   건너뛰고 이 포트로 직접 접속했다"는 확실한 신호이므로, 「규칙
#   미등록」403 대신 게이트웨이 주소로 그대로 돌려보낸다.
#   ⚠️ 인증 확인 **뒤**(로그인 여부를 가린 다음)에 검사한다 — 앞에 두면
#   `test_guard_blocks_unauthenticated_notification_send`(무인증 알림
#   발송 취약점 회귀 시험, `/api/cases/.../notifications`가 RELOCATED에도
#   걸림)가 기대하는 401보다 이 리다이렉트가 먼저 나가 버려, "로그인 없이
#   호출해도 뭔가 응답은 온다"는 정보 자체가 새 나가는 셈이 된다. 인증
#   여부와 무관하게 안전하도록 로그인 확인 뒤 규칙 검사 단계에서 처리한다.
GATEWAY_PORT = os.environ.get("URBANGUARD_GATEWAY_PORT", "8080")

RELOCATED = (
    re.compile(r"^/api/crowd/"),
    re.compile(r"^/api/cases"),
    re.compile(r"^/api/notifications/"),
    re.compile(r"^/api/road/"),
    re.compile(r"^/api/road-analysis/"),
    re.compile(r"^/api/road-report/"),
    re.compile(r"^/api/stream/flood-risk"),
    re.compile(r"^/api/flood-risk"),
    re.compile(r"^/api/flood-history"),
    re.compile(r"^/api/stream/risk"),
    re.compile(r"^/api/risk"),
    re.compile(r"^/api/history"),
    re.compile(r"^/api/report/"),
    re.compile(r"^/api/traffic/roi-flow/propose"),
    # `/media/flood-runs/`는 platform-shell 자신이 그대로 서빙한다
    # (nginx도 이 경로만은 urbanguard_platform으로 돌린다) — 그 외
    # `/media/*`(증거영상 등)는 인파관리 서비스로 옮겨졌다.
    re.compile(r"^/media/(?!flood-runs/)"),
)

V = R.Action.VIEW
E = R.Action.EDIT
X = R.Action.EXECUTE
RQ = R.Action.REQUEST

FLOOD, TRAFFIC, CROWD, ROAD = (R.Domain.FLOOD, R.Domain.TRAFFIC,
                               R.Domain.CROWD, R.Domain.ROAD)

# ★ 2026-08-21 flood/traffic 도메인 분리
#   상황판 스냅샷 API(`/api/risk`, `/api/history`, SSE)는 **한 응답에 침수와
#   교통 값을 함께** 실어 보낸다(`runner._snapshot`). 도메인을 하나만 걸면
#   교통 담당자가 자기 데이터를 못 보고, `None` 으로 열면 인파만 담당하는
#   사용자에게까지 열린다. 그래서 「둘 중 하나라도 담당이면」으로 검사한다
#   (:func:`roles.can` 이 여러 도메인을 받는다).
BOARD = (FLOOD, TRAFFIC)

# (메서드 집합 | None=전체, 경로 정규식, 자원, 행위, 도메인)
# 도메인 자리에는 하나 또는 **여러 개(튜플)**를 둘 수 있다 — 여러 개면
# 「그중 하나라도 담당이면 통과」다(:data:`BOARD` 참고).
# 위에서부터 먼저 일치하는 규칙을 쓴다.
RULES: list[tuple[frozenset[str] | None, re.Pattern, str, R.Action,
                  R.Domain | tuple[R.Domain, ...] | None]] = [
    # --- 멀티뷰 관제 (S-05) ---
    # 도메인을 걸지 않는다 — 한 화면에 침수·인파·노면을 섞어 놓는 것이
    # 이 화면의 목적이다. 담당 도메인 제한은 강조(alerts) 쪽에서 건다.
    (None, re.compile(r"^/api/multiview/"), R.MONITOR, V, None),

    # --- 침수 ---
    (None, re.compile(r"^/api/blocks/?$"), R.MONITOR, V, None),
    # SSE 상황판 스트림. /api/risk 와 같은 내용을 밀어 주므로 권한도 같다.
    # **/api/risk 규칙보다 먼저** 둔다 — 뒤에 두면 그쪽에 먼저 걸린다.
    (None, re.compile(r"^/api/stream/risk/?$"), R.MONITOR, V, BOARD),
    (None, re.compile(r"^/api/risk(/|$)"), R.MONITOR, V, BOARD),
    (None, re.compile(r"^/api/history/?$"), R.MONITOR, V, BOARD),
    (None, re.compile(r"^/api/roi/"), R.MONITOR, V, FLOOD),
    (None, re.compile(r"^/api/record/"), R.MONITOR, V, BOARD),
    (None, re.compile(r"^/api/report/"), R.REPORT, V, BOARD),
    # S-23 분석 결과 이력은 순수 침수(물 세그멘테이션) 전용이다.
    (None, re.compile(r"^/api/flood-runs(/|$)"), R.MONITOR, V, FLOOD),
    # ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — flood-service 전용 신규
    # 경로. `/api/risk`류는 침수·교통이 합쳐 있던 시절의 이름이라
    # platform-shell(옛 PipelineRunner, 롤백 경로)이 계속 쓰고, 이쪽은
    # 침수만 담아 새 이름을 쓴다(§Phase 4 상세 설계 "SSE" 항목).
    # /api/stream/flood-risk 를 /api/flood-risk 보다 먼저 둔다 — 위
    # /api/stream/risk 와 같은 이유(뒤에 두면 그쪽 규칙에 먼저 걸린다).
    (None, re.compile(r"^/api/stream/flood-risk/?$"), R.MONITOR, V, FLOOD),
    (None, re.compile(r"^/api/flood-risk(/|$)"), R.MONITOR, V, FLOOD),
    (None, re.compile(r"^/api/flood-history/?$"), R.MONITOR, V, FLOOD),
    # --- 교통 (2026-08-21 신설) ---
    # 통행 방향 자동 생성 제안(Phase 4-A) — 원래 /settings/cameras/*
    # 밑에 있었는데(그쪽은 카메라 CRUD라 platform-shell에 그대로 둠),
    # 실행 중인 추적기 상태가 있어야 답할 수 있어 traffic-service로
    # 옮기며 새 경로를 받았다. 조회지만 실시간 추적 내부 상태를
    # 들여다보는 것이라 설정 화면과 같은 권한(SETTINGS_OPS)을 요구한다
    # (원래 라우트의 require_page(R.SETTINGS_OPS, VIEW)와 동일).
    (None, re.compile(r"^/api/traffic/roi-flow/propose/?$"),
     R.SETTINGS_OPS, V, TRAFFIC),
    (None, re.compile(r"^/api/traffic(/|$)"), R.MONITOR, V, TRAFFIC),
    # --- 도로 ---
    # 집중 감시(S-44)는 분석을 반복 실행하므로 조회가 아니라 **실행** 권한이다.
    # 일반 /api/road/ 규칙보다 먼저 와야 한다 — 뒤에 두면 조회 권한만으로
    # CPU를 물고 도는 감시를 켤 수 있다.
    (frozenset({"POST"}), re.compile(r"^/api/road/live/focus(/stop)?/?$"),
     R.DETECT, X, ROAD),
    # 학습 데이터 자동 수집 on/off — 도로 영상을 디스크에 쌓기 시작하는
    # 결정이라 관제 권한이 아니라 **시스템 설정** 권한을 요구한다.
    # 켜고 끄기(``/collect``)와 영상에서 프레임 뽑기(``/collect/video``) 모두
    # 같은 데이터셋을 채우므로 같은 권한을 요구한다.
    (frozenset({"POST"}), re.compile(r"^/api/road/collect(/video)?/?$"),
     R.SETTINGS_SYS, E, None),
    (None, re.compile(r"^/api/road/"), R.MONITOR, V, ROAD),
    (None, re.compile(r"^/api/road-analysis/targets/?$"), R.DETECT, V, ROAD),
    (frozenset({"POST"}), re.compile(r"^/api/road-analysis/run/?$"), R.DETECT, X, ROAD),
    (None, re.compile(r"^/api/road-report/"), R.REPORT, V, ROAD),
    # --- 인파 ---
    # 알림 발송이 /api/cases/{id}/notifications 라서 cases 규칙보다 먼저 와야 한다.
    (frozenset({"POST"}), re.compile(r"^/api/cases/[^/]+/notifications/?$"),
     R.NOTIFY, RQ, CROWD),
    (None, re.compile(r"^/api/cases(/|$)"), R.MONITOR, V, CROWD),
    (None, re.compile(r"^/api/crowd/live/?$"), R.MONITOR, V, CROWD),
    # 상시 카메라별 모니터링 (2026-08-27) — 실시간 조회와 같은 권한.
    (None, re.compile(r"^/api/crowd/continuous/?$"), R.MONITOR, V, CROWD),
    (None, re.compile(r"^/api/crowd/cameras/?$"), R.MONITOR, V, CROWD),
    # 관측 이력·기준선. 실시간 조회와 같은 권한이다 — 이력으로 우회해
    # 더 볼 수 있으면 안 된다.
    (None, re.compile(r"^/api/crowd/history/"), R.MONITOR, V, CROWD),
    # 인파 선택 탐지 — 실행이므로 DETECT/EXECUTE 권한을 요구한다.
    (frozenset({"POST"}), re.compile(r"^/api/crowd/analyze/?$"), R.DETECT, X, CROWD),
    (frozenset({"POST"}), re.compile(r"^/api/crowd/source/?$"), R.CROWD_SOURCE, E, CROWD),
    # --- 알림 ---
    (None, re.compile(r"^/api/notifications/"), R.NOTIFY, V, None),
    # --- 증거 영상 (S-88) ---
    # 파일 자체가 개인정보라 열람도 권한을 요구한다.
    (None, re.compile(r"^/admin/evidence"), R.EVIDENCE, V, None),
    # 증거 자료 팝업이 쓰는 메타(ROI·이벤트 순간). 화면과 **같은 권한**이다 —
    # 팝업이 본체보다 헐거우면 그쪽으로 새어 나간다.
    (None, re.compile(r"^/api/evidence/"), R.EVIDENCE, V, None),
    # --- 서비스 재기동 (S-01) ---
    # 재기동은 상시 탐지를 전부 멎게 하는 조작이라 **실행** 권한을 요구한다.
    # SETTINGS_SYS × EXECUTE 는 시스템관리자만 가진다(MGR·OPR 은 빈 집합).
    # 조회(/state)보다 먼저 와야 한다 — 뒤에 두면 조회 규칙이 먼저 걸려
    # 조회 권한만으로 서비스를 내릴 수 있다.
    (frozenset({"POST"}), re.compile(r"^/api/service/restart/?$"),
     R.SETTINGS_SYS, E, None),
    (None, re.compile(r"^/api/service/"), R.SETTINGS_SYS, V, None),
    # --- 서비스 관리(개별 도메인 정지/시작, 2026-09-01 신설) ---
    # 이건 platform-shell 자기 자신의 재기동(위 /api/service/*)이 아니라
    # 4개 도메인 서비스 상태 조회다 — 별개 경로. `/admin/services/{key}/
    # stop|start`(정지·시작 실행)는 `/api/`로 시작하지 않아 여기 등록할
    # 필요가 없다(화면 취급 — 라우트 자체의 require()가 세부 권한을
    # 본다). 오직 이 폴링용 JSON 조회만 `/api/` 밑이라 등록이 필요하다
    # (안 하면 "규칙 미등록" 기본 거부에 걸린다 — 실기로 확인).
    (None, re.compile(r"^/api/admin/services/state/?$"),
     R.SETTINGS_SYS, V, None),
    # --- 미디어 ---
    (None, re.compile(r"^/media/"), R.MONITOR, V, None),
    # --- 화면 ---
    (None, re.compile(r"^/$"), R.DASHBOARD, V, None),
    # 이벤트는 도메인 무관 목록이므로 여기서는 도메인 검사를 하지 않는다.
    # 개별 이벤트 접근 시 라우트가 담당 도메인을 확인한다.
    (None, re.compile(r"^/events(/|$)"), R.EVENT, V, None),
    (None, re.compile(r"^/flood(/|$)"), R.MONITOR, V, FLOOD),
    (None, re.compile(r"^/traffic(/|$)"), R.MONITOR, V, TRAFFIC),
    (None, re.compile(r"^/crowd(/|$)"), R.MONITOR, V, CROWD),
    (None, re.compile(r"^/road(/|$)"), R.MONITOR, V, ROAD),
    (None, re.compile(r"^/facility(/|$)"), R.FACILITY, V, None),
    # 현장 제보 — 도로 도메인. 사진 열람도 같은 권한을 요구한다.
    (None, re.compile(r"^/reports(/|$)"), R.MONITOR, V, ROAD),
    # 분석 — 통계는 감사 권한, 모델 운영은 시스템 설정 권한
    (None, re.compile(r"^/analytics(/|$)"), R.AUDIT, V, None),
    (None, re.compile(r"^/models(/|$)"), R.SETTINGS_SYS, V, None),
    (None, re.compile(r"^/notify/history/?$"), R.NOTIFY_HISTORY, V, None),
    (None, re.compile(r"^/notify(/|$)"), R.NOTIFY, V, None),
    # /admin/*, /settings/* 은 라우트의 require_page 가 세부 권한을 본다.
    # 여기서는 로그인 여부만 확인한다.
    (None, re.compile(r"^/admin(/|$)"), R.DASHBOARD, V, None),
    (None, re.compile(r"^/settings(/|$)"), R.DASHBOARD, V, None),
    # 내 계정 — 로그인한 사람이면 누구나 자기 비밀번호를 바꿀 수 있다.
    (None, re.compile(r"^/account(/|$)"), R.DASHBOARD, V, None),
]


def _match(method: str, path: str):
    for methods, pat, res, act, dom in RULES:
        if methods is not None and method not in methods:
            continue
        if pat.match(path):
            return res, act, dom
    return None


def _is_api(path: str) -> bool:
    return path.startswith("/api/") or path.startswith("/media/")


@dataclass(frozen=True)
class SessionUser:
    """요청 동안 들고 다니는 사용자 정보.

    ORM 객체를 그대로 넘기지 않는 이유는, DB 세션이 닫힌 뒤 지연 로딩이 터지기
    때문이다. 화면·메뉴가 쓰는 속성만 미리 뽑아 둔다. 속성 이름은
    :class:`~.models.User` 와 맞춰 템플릿이 양쪽 모두에서 동작하게 한다.
    """

    id: int
    login_id: str
    name: str
    dept: str
    role: str
    domain_set: set[str] = field(default_factory=set)
    must_change_password: bool = False


# 비밀번호를 바꿔야 하는 계정에게도 열어 두는 경로. 이걸 막으면 비밀번호를
# 바꿀 수도, 로그아웃할 수도 없는 상태가 된다.
PW_CHANGE_ALLOWED = (
    re.compile(r"^/account/password/?$"),
    re.compile(r"^/logout/?$"),
    re.compile(r"^/static/"),
)


def _load_session_user(uid) -> SessionUser | None:
    """세션의 uid로 사용자 조회 — 동기 DB 호출이라 반드시 스레드풀에서 돌려야
    한다(:class:`AuthGuard` 참고)."""
    db = get_session()
    try:
        u = db.get(User, uid)
        if u is not None and u.is_active:
            return SessionUser(id=u.id, login_id=u.login_id, name=u.name,
                               dept=u.dept, role=u.role,
                               domain_set=set(u.domain_set),
                               must_change_password=u.must_change_password)
        return None
    finally:
        db.close()


class AuthGuard(BaseHTTPMiddleware):
    """5개 서비스(main·crowd·road·flood·traffic)가 전부 이 클래스를 그대로
    공유해서 쓴다. ``relocated`` 인자를 넣지 않으면(기본값) 아래 RELOCATED
    리다이렉트 분기는 그냥 통과된다 — 반드시 그래야 한다. 예를 들어
    `/api/crowd/*`는 platform-shell 기준으로는 "다른 서비스로 옮겨간
    경로"지만, crowd-service 자기 자신에게는 "내가 처리해야 할 그 경로
    자체"다. platform-shell(`service/main.py`)만
    ``add_middleware(AuthGuard, relocated=RELOCATED)``로 명시해 켠다.
    """

    def __init__(self, app, relocated: tuple = ()) -> None:
        super().__init__(app)
        self._relocated = relocated

    async def dispatch(self, request, call_next):
        path = request.url.path
        if any(p.match(path) for p in PUBLIC):
            return await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        data = read_session(token) if token else None
        user = None
        if data:
            # ⚠️ 실기 확인(2026-09-01, py-spy 라이브 스택으로 직접 포착) —
            # 이 조회가 원래 `await` 없이 이 async dispatch() 안에서 그대로
            # 동기 호출됐다. 미들웨어는 **모든** 요청(정적 파일 등 PUBLIC
            # 제외 전부)을 거치는데, uvicorn이 워커 1개(비동기 이벤트루프
            # 하나)로 도는 이 프로젝트 구조에서 동기 DB 호출을 그대로 두면
            # 그 호출이 끝날 때까지 **프로세스 전체**(다른 모든 연결·새 접속
            # 포함)가 멈춘다. 평소엔 PK 단건 조회라 체감이 안 되지만,
            # `pool_pre_ping=True`(core/db.py)가 유휴 연결마다 핑을 먼저
            # 날리고, CPU가 100%(4개 실시간 탐지 파이프라인)로 Postgres 응답이
            # 늦어지는 구간과 겹치면 그 지연이 그대로 서버 전체 정지로
            # 번진다 — platform-shell이 새 요청의 약 23%를 5초 안에 못
            # 받던 현상의 실제 원인이었다(라이브 스택에서 MainThread가
            # `psycopg._execute_send`에 2초 넘게 멈춰 있는 것을 직접 확인).
            # 게이트웨이를 거치지 않고 8033을 직접 두드리는 정체불명의
            # 폴러(1초 간격)도 매번 이 경로를 타 상시로 이 위험을 키우고
            # 있었다. `run_in_threadpool`로 이벤트루프 밖에서 돌려 해결한다.
            user = await run_in_threadpool(_load_session_user, data.get("uid"))

        if user is None:
            if _is_api(path):
                return JSONResponse({"detail": "로그인이 필요합니다."}, status_code=401)
            nxt = path if path != "/" else "/"
            return RedirectResponse(url=f"/login?next={nxt}", status_code=303)

        # 임시 비밀번호 상태면 비밀번호 변경 화면 밖으로 나가지 못한다.
        if user.must_change_password and not any(p.match(path)
                                                 for p in PW_CHANGE_ALLOWED):
            if _is_api(path):
                return JSONResponse(
                    {"detail": "비밀번호를 변경해야 이용할 수 있습니다."}, status_code=403)
            return RedirectResponse(url="/account/password", status_code=303)

        if any(p.match(path) for p in self._relocated):
            # ⚠️ RULES 표엔 이 경로들의 옛 규칙이 아직 남아 있다(도메인
            # 분리 전 것 — 지우면 각 서비스가 자기 프로세스 안에서 같은
            # 표를 또 쓰므로 위험). 그래서 `_match()`가 여전히 규칙을
            # "찾아 버려" 아래 `rule is None` 분기(403)까지 못 간다 —
            # 그대로 두면 FastAPI가 없는 라우트라 그냥 404를 내 사용자가
            # "왜 안 되는지" 전혀 알 수 없다. 규칙 검사보다 먼저 게이트웨이로
            # 돌려보낸다.
            target = request.url.replace(port=int(GATEWAY_PORT))
            log.info("게이트웨이 우회 직접 접속 감지 → 리다이렉트: %s → %s",
                     request.url, target)
            return RedirectResponse(url=str(target), status_code=307)

        rule = _match(request.method, path)
        if rule is None:
            if _is_api(path):
                # 기본 거부. 규칙 등록을 잊은 새 API가 조용히 열리는 것을 막는다.
                log.warning("규칙 미등록 경로 차단: %s %s (login_id=%s)",
                            request.method, path, user.login_id)
                # 이건 사용자의 권한 문제가 아니라 **우리 설정의 결함**이다.
                # 경고 로그만 남기면 아무도 안 보므로 오류 관리(S-92)에 올린다.
                # 지연 임포트 — errors 가 guard 를 다시 부르지는 않지만,
                # 임포트 고리를 만들지 않는 편이 안전하다.
                # ⚠️ 실기 확인(2026-09-01) — 위 `_load_session_user`와 같은
                # 이유로 이 동기 DB 쓰기도 `run_in_threadpool`로 돌린다.
                # 이 미들웨어 함수 전체가 이벤트루프 한 개짜리 프로세스
                # 안에서 실행돼, 여기서 그대로 블로킹하면 규칙 미등록
                # 경로가 한 번만 눌려도 서버 전체가 멈춘다(py-spy로 직접
                # 포착 — MainThread가 이 `errors.record()` 안의 SQLAlchemy
                # 호출에 멈춰 있었다).
                from . import errors as _errors
                await run_in_threadpool(
                    _errors.record,
                    code="UG-AUTH-005",
                    message=f"권한 규칙 미등록 경로 차단: {request.method} {path}",
                    source="web", path=path, method=request.method,
                    status_code=403, login_id=user.login_id, severity="error")
                return JSONResponse(
                    {"detail": "권한 규칙이 등록되지 않은 경로입니다."}, status_code=403)
            # 화면은 로그인만 확인하고 통과시킨다(라우트 의존성이 세부 권한을 본다).
            request.state.user = user
            return await call_next(request)

        res, act, dom = rule
        if not R.can(user.role, res, act, domain=dom, user_domains=user.domain_set):
            if _is_api(path):
                return JSONResponse({"detail": "이 기능에 대한 권한이 없습니다."},
                                    status_code=403)
            return JSONResponse({"detail": "이 화면에 대한 권한이 없습니다."},
                                status_code=403)

        request.state.user = user
        return await call_next(request)
