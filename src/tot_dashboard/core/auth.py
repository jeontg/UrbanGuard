"""FastAPI 인증·권한 의존성.

사용법::

    @app.get("/api/risk")
    def api_risk(_=Depends(require(roles.MONITOR, roles.Action.VIEW,
                                   domain=roles.Domain.FLOOD))):
        ...

HTML 화면과 API의 미인증 처리 방식이 다르다. 화면은 로그인 페이지로 보내고,
API는 401 JSON을 준다. 브라우저가 fetch 응답으로 받은 HTML 로그인 페이지를
데이터로 오해하는 문제를 막기 위해서다.
"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from . import roles as R
from .db import get_session
from .models import User
from .security import COOKIE_NAME, read_session


class LoginRequired(Exception):
    """화면 요청에서 미인증. 예외 핸들러가 로그인 페이지로 보낸다."""


def get_db() -> Iterator[Session]:
    db = get_session()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> str:
    # 리버스 프록시 뒤에 놓이면 X-Forwarded-For 가 실제 접속지다. 지자체 배포는
    # 대개 프록시를 거치므로 이 헤더를 우선한다.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return getattr(request.client, "host", "") or ""


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """로그인 상태면 사용자, 아니면 None. 화면 분기용."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    user = db.get(User, data.get("uid"))
    if user is None or not user.is_active:
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = optional_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="로그인이 필요합니다.")
    return user


def require(resource: str, action: R.Action | str, *,
            domain: R.Domain | str | None = None):
    """(자원, 행위) 권한을 요구하는 의존성을 만든다."""

    def _dep(user: User = Depends(current_user)) -> User:
        if not R.can(user.role, resource, action,
                     domain=domain, user_domains=user.domain_set):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="이 기능에 대한 권한이 없습니다.")
        return user

    return _dep


def require_page(resource: str, action: R.Action | str, *,
                 domain: R.Domain | str | None = None):
    """화면용. 미인증이면 로그인 페이지로 보내고, 권한 부족이면 403."""

    def _dep(request: Request, db: Session = Depends(get_db)) -> User:
        user = optional_user(request, db)
        if user is None:
            raise LoginRequired(request.url.path)
        if not R.can(user.role, resource, action,
                     domain=domain, user_domains=user.domain_set):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="이 화면에 대한 권한이 없습니다.")
        return user

    return _dep


def login_redirect(request: Request) -> RedirectResponse:
    nxt = request.url.path
    return RedirectResponse(url=f"/login?next={nxt}", status_code=303)


def menu_for(user: User | None) -> list[dict]:
    """역할에 따라 보이는 메뉴만 추려 준다.

    설계서 2절의 2단계 메뉴 구조. 권한 없는 항목은 비활성이 아니라 아예
    렌더링하지 않는다 — 있는데 못 누르는 메뉴는 운영자에게 혼란만 준다.
    """
    if user is None:
        return []
    ud = user.domain_set
    role = user.role

    def ok(res: str, act: R.Action, dom: R.Domain | None = None) -> bool:
        return R.can(role, res, act, domain=dom, user_domains=ud)

    home_items = [{"key": "dashboard", "label": "통합 상황판 (S-01)", "href": "/"}]
    # 멀티뷰는 관제요원이 하루 종일 보는 화면이라 상황판 바로 옆에 둔다
    # (경남 제안요청서 SFR-001·SFR-003).
    if ok(R.MONITOR, R.Action.VIEW):
        home_items.append({"key": "multiview", "label": "멀티뷰 관제 (S-05)",
                           "href": "/multiview"})
    groups: list[dict] = [{
        "key": "home", "label": "관제", "icon": "◎", "items": home_items,
    }]
    if ok(R.EVENT, R.Action.VIEW):
        ev_items = [{"key": "events", "label": "이벤트 목록 (S-02)",
                     "href": "/events"},
                    # 판정은 이벤트를 다루는 일이라 이벤트 묶음에 둔다.
                    {"key": "feedback", "label": "탐지 피드백 (S-07)",
                     "href": "/feedback"}]
        # 교대 인수인계를 이벤트 묶음에 둔다 — 인계문의 알맹이가 「지금 열려
        # 있는 이벤트」라서, 설정이 아니라 근무 화면 곁에 있어야 한다.
        if ok(R.HANDOVER, R.Action.VIEW):
            ev_items.append({"key": "handover", "label": "교대 인수인계 (S-04)",
                             "href": "/handover"})
        groups.append({"key": "events", "label": "이벤트", "icon": "◆",
                       "items": ev_items})

    # ★ 2026-08-24(재정리) — 「대응」(시설물 원격제어 1항목뿐이던 묶음)과
    # 「알림·통보」를 합친다. 실제 업무에서 조치(시설물 제어)·승인(알림
    # 대기함)·확인(통보 이력)은 한 사건을 처리하는 한 줄기 흐름인데, 예전
    # 구조는 이 셋을 서로 다른 대분류로 갈라 놓아 사건 하나를 처리하려면
    # 메뉴를 오가야 했다(설계서 3절 "한 호흡 완결 — 화면 이동이 골든타임을
    # 깎는다" 원칙과 어긋남). 이벤트 바로 다음 자리에 둔 것도 같은 이유
    # — 사건을 고른 다음(이벤트) 곧바로 조치·통보로 이어지게 한다.
    resp_notify_items = []
    if ok(R.FACILITY, R.Action.VIEW):
        resp_notify_items.append({"key": "facility",
                                  "label": "시설물 원격제어 (S-11)",
                                  "href": "/facility"})
    if ok(R.NOTIFY, R.Action.VIEW):
        resp_notify_items.append({"key": "notify", "label": "승인 대기함 (S-50)",
                                  "href": "/notify"})
    if ok(R.NOTIFY_HISTORY, R.Action.VIEW):
        resp_notify_items.append({"key": "notify-history",
                                  "label": "통보 이력 (S-51)",
                                  "href": "/notify/history"})
    if resp_notify_items:
        groups.append({"key": "response", "label": "대응·통보", "icon": "▲",
                       "items": resp_notify_items})

    # 도메인 화면은 v4 IA에서 「심층 분석용」으로 위치가 내려갔다. 상시 관제는
    # 통합 상황판과 이벤트 목록에서 이뤄진다(docs/ui_design_spec.md 4절).
    # 2026-08-21 flood/traffic 도메인 분리 — 「교통위험」 그룹 신설.
    # ⚠️ 화면번호(S-2x)는 UI 설계서에 아직 배정되지 않았다 — 배정되면
    #    docs/ui_design_spec.md 와 함께 채운다.
    #
    # ★ 2026-08-24 — 그동안 도메인 전용 설정 화면(위험도 임계값 등)이
    #   "설정" 대분류에 뭉뚱그려 있어, 이름은 도메인 전체에 적용되는 것처럼
    #   보이는데 실제로는 특정 도메인 전용인 항목들을 찾기 어려웠다
    #   (사용자 지적). 각 도메인 그룹 끝에 그 도메인 "전용" 설정 화면을
    #   추가한다 — 항목마다 필요한 권한(res)을 함께 적어 두고, 그 도메인에
    #   대해서까지 권한이 있어야만 보이게 한다(``extra`` 목록).
    #   여러 도메인이 함께 쓰는 화면(위험등급 관리 S-95 등)은 "카메라·지점
    #   관리" 그룹에도 그대로 남기고, 여기서는 **링크만 추가**한다 — 같은
    #   화면이 여러 그룹에 나타나는 것은 의도된 것이다(사용자가 어느
    #   경로로 들어오든 자기 도메인 설정을 찾을 수 있게).
    dom_items = [
        (R.Domain.FLOOD, "flood", "침수", [
            ("flood-monitor", "실시간 관제 (S-20)", "/flood"),
            ("flood-runs", "분석 결과 이력 (S-23)", "/flood/runs")], [
            # routes_config.py 에 이미 domain=FLOOD 로 고정돼 있다 — 침수
            # 전용인데 "설정" 대분류에 있어 이름만 보고는 알 수 없었다.
            ("set-threshold", "위험도 임계값 (S-82)", "/settings/threshold",
             R.SETTINGS_OPS),
            ("set-alert", "알림 규칙 (S-83)", "/settings/alert", R.SETTINGS_OPS),
            # 2026-08-25 신설 — "AI 모델 학습"은 "위험등급 관리(S-95)"와
            # 같은 이유로 4개 도메인 그룹에 전부 링크를 둔다(사용자 요청 —
            # 담당자가 자기 도메인에서 바로 재학습을 찾을 수 있게). ⚠
            # "위험등급 관리"와 달리 도메인마다 화면 내용(탭)이 다르므로
            # key 는 도메인별로 따로 둔다 — 공유하면 침수 탭을 보고 있는데
            # 교통위험 메뉴까지 강조되는 오작동이 생긴다(2026-08-25 실사용
            # 중 발견: "교통위험에서도 침수 학습 화면이 나타난다").
            ("set-training-flood", "AI 모델 학습", "/settings/training?domain=flood",
             R.SETTINGS_SYS),
        ]),
        (R.Domain.TRAFFIC, "traffic", "교통위험", [
            ("traffic-monitor", "실시간 관제", "/traffic")], [
            # 2026-08-24 신설 — 침수의 위험도 임계값(S-82)과 대칭인 교통
            # 전용 화면(routes_config.py SCREENS["traffic"]). 화면번호는
            # 아직 미배정.
            ("set-traffic", "위험도 임계값", "/settings/traffic", R.SETTINGS_OPS),
            # 위험등급 관리(S-95)는 침수·인파·노면 3개 도메인 공용 화면이지만,
            # 교통위험 알림 발동 심각도(traffic_notify_min_severity)도 같은
            # 화면에 있어 링크를 추가한다.
            ("set-levels", "위험등급 관리 (S-95)", "/settings/levels",
             R.SETTINGS_OPS),
            ("set-training-traffic", "AI 모델 학습", "/settings/training?domain=traffic",
             R.SETTINGS_SYS),
        ]),
        (R.Domain.CROWD, "crowd", "인파관리", [
            ("crowd-monitor", "실시간 관제 (S-30)", "/crowd"),
            ("crowd-cases", "분석 사례 (S-32)", "/crowd/cases")], [
            ("set-levels", "위험등급 관리 (S-95)", "/settings/levels",
             R.SETTINGS_OPS),
            ("set-training-crowd", "AI 모델 학습", "/settings/training?domain=crowd",
             R.SETTINGS_SYS),
        ]),
        (R.Domain.ROAD, "road", "도로 노면 관리", [
            ("road-status", "노면 현황 (S-40)", "/road"),
            ("road-detect", "AI 탐지 실행 (S-41)", "/road/detect"),
            ("reports", "현장 제보 (S-43)", "/reports")], [
            ("set-levels", "위험등급 관리 (S-95)", "/settings/levels",
             R.SETTINGS_OPS),
            ("set-training-road", "AI 모델 학습", "/settings/training?domain=road",
             R.SETTINGS_SYS),
        ]),
    ]
    for dom, key, label, items, extra in dom_items:
        if ok(R.MONITOR, R.Action.VIEW, dom):
            full_items = [{"key": k, "label": l, "href": h} for k, l, h in items]
            for k, l, h, res in extra:
                if ok(res, R.Action.VIEW, dom):
                    full_items.append({"key": k, "label": l, "href": h})
            groups.append({"key": key, "label": label, "icon": "▣",
                           "items": full_items})

    # 통계·성과 리포트만 남는다 — AI 모델 운영은 "AI 모델 관리"로 이동
    # (아래, 2026-08-24) 했다. 성격이 같은 화면 3개가 "분석"·"설정" 두
    # 그룹에 흩어져 있던 것을 한 곳으로 모은 것이다(사용자 지적).
    if ok(R.AUDIT, R.Action.VIEW):
        groups.append({"key": "analytics", "label": "분석", "icon": "◱", "items": [
            {"key": "analytics", "label": "통계·성과 리포트 (S-60)",
             "href": "/analytics"},
        ]})

    # ★ 2026-08-24 재정리 — 8/24에 "설정"(12항목)·"관리자"(6항목) 2개
    #   그룹을 6개로 쪼갰으나(찾기 쉽게 하려던 것), **묶음 헤더가 늘어난
    #   만큼 세로 길이는 오히려 늘었다**(사용자 지적: 다시 정리하며 논리·
    #   업무 효율 검토). 화면 설계서의 최소주의 원칙("항상 보이는 영역"에
    #   적용)에 따라 성격이 비슷한 것끼리 3개로 재통합한다. 권한 게이트는
    #   그대로 두고 배치만 바꿨다 — `docs/202608241437/` 검토안 반영.

    # 운영 설정 — 구축·점검 때 쓰는 화면 9종(카메라·지점 관리 4 + SOP
    # 관리 2 + AI 모델 관리 3을 통합). 관제요원은 근무 중 거의 열지 않는다.
    ops_items = []
    if ok(R.SETTINGS_OPS, R.Action.VIEW):
        # 위험등급·방향·상하류 3종은 routes_levels.py 가 "관계 모델(Urban
        # Ontology)을 손으로 다루는 자리"로 한데 묶어 설명한다 — CCTV
        # 관리도 카메라라는 같은 물리적 대상을 다뤄 함께 둔다.
        ops_items += [
            {"key": "set-cameras", "label": "CCTV 관리 (S-80·81)",
             "href": "/settings/cameras"},
            {"key": "set-levels", "label": "위험등급 관리 (S-95)",
             "href": "/settings/levels"},
            {"key": "set-aim", "label": "지점 방향 관리 (S-96)",
             "href": "/settings/aim"},
            {"key": "set-flow", "label": "상·하류 관리 (S-97)",
             "href": "/settings/flow"},
        ]
    if ok(R.SOP, R.Action.VIEW):
        # SOP 는 SETTINGS_OPS 와 권한이 다르다 — 공통 단계가 있어 도메인
        # 으로 자를 수 없다(roles.SOP 주석 참고). 단계를 만드는 곳(S-86)과
        # 붙이는 곳(S-98)을 나란히 둔다.
        ops_items += [
            {"key": "set-sop", "label": "디지털 SOP (S-86)",
             "href": "/settings/sop"},
            {"key": "set-hazard-sop", "label": "위험유형별 SOP (S-98)",
             "href": "/settings/hazard-sop"},
        ]
    if ok(R.SETTINGS_SYS, R.Action.VIEW):
        # "AI 모델 운영(S-61)"·"AI 모델(S-84)"·"AI 모델·침수(S-84)" 셋이
        # 이름은 다 "AI 모델"인데 예전엔 "분석"·"설정" 두 그룹에 흩어져
        # 있었다. 화면번호도 두 화면이 S-84 로 중복돼 있고(docs/
        # pending_tasks.md 확인 필요), 메뉴 이름을 각 화면 제목에 맞춰
        # 정정하며 한 자리로 모은다.
        ops_items += [
            {"key": "models", "label": "AI 모델 운영 (S-61)", "href": "/models"},
            {"key": "set-model", "label": "AI 모델 설정 · 물 세그멘테이션 (S-84)",
             "href": "/settings/model"},
            {"key": "set-model-flood",
             "label": "AI 모델 설정 · 침수 파이프라인 (S-84)",
             "href": "/settings/model-flood"},
            # 2026-08-25 신설 — 4개 도메인 그룹에도 같은 링크가 있다.
            # "AI 모델 운영·설정" 바로 옆에 두는 이유 — 학습 결과를 운영에
            # 반영하려면 결국 이 화면들로 다시 와야 한다(학습=운영 반영이
            # 아니다). 여기 링크는 도메인을 지정하지 않아 화면이 기본값
            # (침수)을 연다 — key 를 "set-training-flood"로 맞춰, 침수
            # 탭을 보고 있을 때는 여기와 침수 그룹 링크가 함께 강조된다.
            {"key": "set-training-flood", "label": "AI 모델 학습 (신규)",
             "href": "/settings/training"},
        ]
    if ops_items:
        groups.append({"key": "ops-settings", "label": "운영 설정", "icon": "⚙",
                       "items": ops_items})

    # 기록·감사 — "사후에 들여다보는" 성격이 같은 화면들. 감사 로그를
    # 증거 영상·반출대장과 함께 두면 부서담당자에게 "계정·감사" 묶음이
    # 감사 로그 1개만 남던 문제도 해소된다.
    record_items = []
    if ok(R.AUDIT, R.Action.VIEW):
        record_items.append({"key": "audit", "label": "감사 로그 (S-91)",
                             "href": "/admin/audit"})
    # 반출 대장은 OPR 도 열람한다 — 「누구에게 나갔는지」는 상황실도 알아야 한다.
    if ok(R.EVIDENCE, R.Action.VIEW):
        record_items.append({"key": "evidence", "label": "증거 영상 관리 (S-88)",
                             "href": "/admin/evidence"})
    if ok(R.VIDEO_DISCLOSURE, R.Action.VIEW):
        record_items.append({"key": "disclosure",
                             "label": "영상 반출 관리대장 (S-94)",
                             "href": "/admin/disclosure"})
    if record_items:
        groups.append({"key": "records", "label": "기록·감사", "icon": "◇",
                       "items": record_items})

    # 시스템 관리 — 시스템관리자(SYS) 전용 계정·오류·서버·기관 설정.
    sysadm_items = []
    if ok(R.USERS, R.Action.VIEW):
        sysadm_items.append({"key": "users", "label": "사용자·권한 관리 (S-90)",
                             "href": "/admin/users"})
    if ok(R.SYSTEM_ERROR, R.Action.VIEW):
        sysadm_items += [
            {"key": "errors", "label": "오류 관리 (S-92)", "href": "/admin/errors"},
            {"key": "error-codes", "label": "오류 코드 사전 (S-93)",
             "href": "/admin/error-codes"},
        ]
    if ok(R.SETTINGS_SYS, R.Action.VIEW):
        sysadm_items += [
            {"key": "set-org", "label": "기관 정보·화면 (S-85)",
             "href": "/settings/org"},
            # 리눅스·윈도우 양쪽 납품이라 재기동 방식이 갈린다(S-87).
            {"key": "set-server", "label": "서버 운영 (S-87)",
             "href": "/settings/server"},
            # 2026-09-01 사용자 요청 신설 — 화면번호 미배정("위험도
            # 임계값(교통)" 등 다른 항목도 같은 이유로 번호가 없다).
            # 2026-08-31 도메인 분리로 서비스가 5개(플랫폼-쉘+4개
            # 도메인)로 늘었는데, "지금 다 살아 있는가"를 볼 화면이
            # 없었다 — routes_services.py 머리말 참고.
            {"key": "admin-services", "label": "서비스 관리",
             "href": "/admin/services"},
        ]
    if sysadm_items:
        groups.append({"key": "system", "label": "시스템 관리", "icon": "❖",
                       "items": sysadm_items})

    # 이벤트·대응·알림·분석·연계·설정 대분류는 해당 화면 구현 시 추가한다.
    # 동작하지 않는 메뉴를 미리 노출하면 운영자에게 혼란만 준다.
    return groups
