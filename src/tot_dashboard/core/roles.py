"""권한 매트릭스 — docs/ui_design_spec.md 4절을 코드로 옮긴 것.

설계서의 표가 이 파일의 :data:`PERMISSIONS` 다. 표를 고치면 이 파일을 고치고,
이 파일을 고치면 설계서를 고쳐야 한다. 두 곳이 어긋나면 설계서가 기준이다.

권한은 (자원, 행위) 쌍으로 판정한다. 화면 단위가 아니라 자원 단위인 이유는
한 화면이 여러 API를 쓰고, 같은 API를 여러 화면이 공유하기 때문이다.
"""
from __future__ import annotations

import os
from enum import Enum


class Role(str, Enum):
    SYS = "SYS"   # 시스템관리자 — 정보통신 부서
    MGR = "MGR"   # 부서담당자 — 안전총괄·도로 부서
    OPR = "OPR"   # 관제요원 — 상황실 근무자


ROLE_LABELS = {Role.SYS: "시스템관리자", Role.MGR: "부서담당자", Role.OPR: "관제요원"}


class Domain(str, Enum):
    """★ 2026-08-21 「침수·교통위험」을 침수와 교통 2개로 나눴다.

    예전에는 ``FLOOD`` 하나가 침수와 교통위험을 함께 뜻했다(라벨 자체가
    「침수·교통위험」이었다). 관제요원이 화면에서 「이게 침수 때문인지
    정체 때문인지」 구분할 수 없었고, 위험도 계산·이벤트·알림도 전부
    한 덩어리였다 — ``docs/202608210801`` 참고.
    """

    FLOOD = "flood"       # 침수 (물 세그멘테이션 + 하천 수위)
    TRAFFIC = "traffic"   # 교통위험 (강우 × 정체 × 정지차량)
    CROWD = "crowd"       # 인파관리
    ROAD = "road"         # 도로 노면 관리


DOMAIN_LABELS = {Domain.FLOOD: "침수", Domain.TRAFFIC: "교통위험",
                 Domain.CROWD: "인파관리", Domain.ROAD: "도로 노면 관리"}

# 짧은 이름 — 엑셀 열 제목(「침수사용」)처럼 **긴 라벨을 붙이면 읽기 어려운
# 자리**에 쓴다. 값 문자열을 키로 둔 것은 쓰는 쪽이 대부분 enum 이 아니라
# DB 문자열을 들고 있기 때문이다.
DOMAIN_SHORT = {Domain.FLOOD.value: "침수", Domain.TRAFFIC.value: "교통",
                Domain.CROWD.value: "인파", Domain.ROAD.value: "노면"}


class Action(str, Enum):
    VIEW = "view"
    EDIT = "edit"
    EXECUTE = "execute"
    REQUEST = "request"
    APPROVE = "approve"


# 자원 키. 설계서의 화면 ID와 1:1은 아니며, 여러 화면이 한 자원을 공유한다.
DASHBOARD = "dashboard"              # S-01 통합 상황판
EVENT = "event"                      # S-02 / S-03 이벤트 목록·상세 (도메인 종속)
NOTIFY_AGENCY = "notify_agency"      # 112·119·재난상황실 통보
FACILITY = "facility"                # S-11 시설물 제어
FACILITY_MODE = "facility_mode"      # S-11 제어 모드 변경 (시스템관리자 전용)
MONITOR = "monitor"                  # S-10 / S-20 / S-30 관제 (도메인 종속)
REPORT = "report"                    # S-12 사건 브리핑·보고서
DETECT = "detect"                    # S-31 AI 탐지 실행
CROWD_SOURCE = "crowd_source"        # S-23 인파 데이터 소스 전환
NOTIFY = "notify"                    # S-40 알림 발송
NOTIFY_HISTORY = "notify_history"    # S-41 알림 발송 이력
SETTINGS_OPS = "settings_ops"        # S-50~S-53 지점·ROI·임계값·알림규칙 (도메인 종속)
SETTINGS_SYS = "settings_sys"        # S-54 / S-55 AI 모델·기관 정보
USERS = "users"                      # S-60 사용자·권한 관리
AUDIT = "audit"                      # S-61 감사 로그
# S-92 오류 관리 — 오류 코드 사전과 발생 이력.
#
# 시스템관리자 전용이다(사용자 지정). 오류 메시지에는 내부 경로·질의·스택트레이스가
# 그대로 실려 오므로, 열람 범위를 넓히면 시스템 내부 구조가 함께 퍼진다.
SYSTEM_ERROR = "system_error"

# S-94 영상 반출 관리대장 — 누구에게 어떤 영상을 언제 왜 내줬는가.
#
# 「지방자치단체 영상정보처리기기 통합관제센터 구축 및 운영 규정」과 개인정보
# 보호법이 요구하는 기록이다. **감사 성격이라 지우지 못하게 한다** — 잘못
# 적었으면 정정 사유를 남기고 새로 적는다(video_disclosure.py 참고).
#
# MGR 도 열람은 되어야 한다. 반출 요청이 실제로 오는 곳이 부서이기 때문이다.
VIDEO_DISCLOSURE = "video_disclosure"

# S-86 디지털 SOP — 도메인·등급별 조치 단계 정의.
#
# **도메인 종속으로 두지 않았다.** 도메인·등급과 무관하게 모든 이벤트에 붙는
# 공통 단계(「영상으로 현장 확인」 등)가 있어, MGR 의 담당 도메인으로 자르면
# 공통 단계를 아무도 못 고치게 된다.
#
# 이행 체크는 이 자원이 아니라 EVENT/EDIT 로 판정한다 — SOP 를 따르는 것은
# 이벤트 처리 행위이고, 그 권한은 관제요원에게 이미 있다.
SOP = "sop"

# S-04 교대 인수인계 — 근무조가 바뀌는 이음매의 기록.
#
# **관제요원이 주체인 유일한 자원이다.** 교대는 상황실 근무자끼리 하는 일이라
# EDIT(작성·제출)와 APPROVE(인수 확인)를 둘 다 OPR 에게 준다. 부서담당자에게
# APPROVE 만 주면 밤에 넘긴 인계문을 아침에 아무도 확인하지 못한다.
#
# 도메인 종속이 아니다 — 관제요원은 도메인별로 근무하지 않는다
# (DOMAIN_SCOPED_ROLES 주석 참고).
HANDOVER = "handover"

# S-88 증거 영상 — 탐지 이벤트의 정지영상·클립.
#
# ⚠️ **파일 자체가 개인정보다.** 사람과 차량이 찍힌다. 다만 관제요원은 이미
# 같은 화면을 실시간으로 보고 있으므로 **열람은 막지 않는다** — 막으면
# 자기가 처리한 이벤트의 근거를 못 보게 된다.
#
# **삭제는 되돌릴 수 없어 부서담당자 이상**으로 제한한다. 등급 설정은
# 저장 정책이라 SETTINGS_SYS(시스템관리자)가 따로 맡는다.
EVIDENCE = "evidence"

# 담당 도메인 범위 검사를 받는 자원.
DOMAIN_SCOPED = {MONITOR, REPORT, DETECT, CROWD_SOURCE, NOTIFY, SETTINGS_OPS,
                 EVENT}

# 도메인 범위 검사를 받는 **역할**은 MGR 뿐이다.
#
# SYS는 전 도메인이고, OPR도 전 도메인이다 — 관제요원은 도메인별로 근무하지
# 않고 한 사람이 침수·인파·노면을 동시에 본다(docs/ui_design_spec.md 3절
# 「이벤트 중심」 원칙). OPR에 도메인 제한을 걸면 상황실 근무 자체가 불가능해진다.
# 부서담당자(MGR)만 소관 업무가 나뉘므로 그쪽에만 적용한다.
DOMAIN_SCOPED_ROLES = {Role.MGR}

_ALL = {Action.VIEW, Action.EDIT, Action.EXECUTE, Action.REQUEST, Action.APPROVE}

PERMISSIONS: dict[Role, dict[str, set[Action]]] = {
    Role.SYS: {
        DASHBOARD: _ALL, EVENT: _ALL, NOTIFY_AGENCY: _ALL,
        FACILITY: _ALL, FACILITY_MODE: _ALL,
        MONITOR: _ALL, REPORT: _ALL, DETECT: _ALL,
        CROWD_SOURCE: _ALL, NOTIFY: _ALL, NOTIFY_HISTORY: _ALL,
        SETTINGS_OPS: _ALL, SETTINGS_SYS: _ALL, USERS: _ALL, AUDIT: _ALL,
        SYSTEM_ERROR: _ALL, VIDEO_DISCLOSURE: _ALL, SOP: _ALL,
        HANDOVER: _ALL, EVIDENCE: _ALL,
    },
    Role.MGR: {
        DASHBOARD: {Action.VIEW},
        # EDIT = 확인·조치기록, APPROVE = 이벤트 종결
        EVENT: {Action.VIEW, Action.EDIT, Action.APPROVE},
        NOTIFY_AGENCY: {Action.VIEW, Action.REQUEST},
        # VIEW=상태조회, REQUEST=차단권고, EXECUTE=원격제어요청
        FACILITY: {Action.VIEW, Action.REQUEST, Action.EXECUTE},
        FACILITY_MODE: set(),   # 모드 변경은 시스템관리자 전용
        MONITOR: {Action.VIEW, Action.EDIT},
        REPORT: {Action.VIEW, Action.EDIT},          # 대외 배포용 확정까지 가능
        DETECT: {Action.VIEW, Action.EXECUTE},
        CROWD_SOURCE: {Action.VIEW, Action.EDIT},
        NOTIFY: {Action.VIEW, Action.REQUEST, Action.APPROVE},
        NOTIFY_HISTORY: {Action.VIEW},
        SETTINGS_OPS: {Action.VIEW, Action.EDIT},    # 담당 도메인에 한해
        SETTINGS_SYS: set(),
        USERS: set(),
        AUDIT: {Action.VIEW},                        # 본인 부서 로그만 (질의에서 제한)
        SYSTEM_ERROR: set(),                         # 시스템관리자 전용
        # 반출 요청이 실제로 오는 곳이 부서다. 등록까지 가능해야 대장이 산다.
        VIDEO_DISCLOSURE: {Action.VIEW, Action.EDIT},
        # 기관 행동매뉴얼을 아는 사람은 부서담당자다. 편집 주체로 둔다.
        SOP: {Action.VIEW, Action.EDIT},
        # 부서담당자도 쓰고 확인한다 — 주간에 부서담당자가 상황실을 맡는
        # 기관이 있어 작성 주체를 관제요원으로만 좁히면 그날 기록이 빈다.
        HANDOVER: {Action.VIEW, Action.EDIT, Action.APPROVE},
        # 삭제(EDIT)까지 가능하다 — 보관 판단은 업무 소관 부서가 한다.
        EVIDENCE: {Action.VIEW, Action.EDIT},
    },
    Role.OPR: {
        DASHBOARD: {Action.VIEW},
        # 확인·조치기록은 가능하되 종결(APPROVE)은 불가 — 상태 확정은 MGR.
        EVENT: {Action.VIEW, Action.EDIT},
        # 기관 통보는 관제요원 단독. 운영 규정상 발견 즉시 통보 의무가 있어
        # 승인 절차를 넣으면 규정과 충돌한다(설계서 6-3절).
        NOTIFY_AGENCY: {Action.VIEW, Action.REQUEST},
        # 차단 「권고」는 알림의 변형이라 관제요원도 보낼 수 있다.
        # 원격 제어 요청(EXECUTE)은 시설을 실제로 움직이는 행위라 제외한다.
        FACILITY: {Action.VIEW, Action.REQUEST},
        FACILITY_MODE: set(),
        MONITOR: {Action.VIEW},
        REPORT: {Action.VIEW},                       # 조회·출력만, 확정 불가
        DETECT: {Action.VIEW, Action.EXECUTE},       # 실행은 가능, 보수요청 전환은 MGR
        CROWD_SOURCE: set(),
        NOTIFY: {Action.VIEW, Action.REQUEST},       # 요청만, 승인 불가
        NOTIFY_HISTORY: {Action.VIEW},
        SETTINGS_OPS: set(),
        SETTINGS_SYS: set(),
        USERS: set(),
        AUDIT: set(),
        SYSTEM_ERROR: set(),                         # 시스템관리자 전용
        # 반출 대장은 **열람만**. 실제로 영상을 내주는 결정은 부서·기관이
        # 하며, 관제요원은 「누구에게 나갔는지」를 확인할 수 있으면 된다.
        VIDEO_DISCLOSURE: {Action.VIEW},
        # 읽고 따르되 고치지는 않는다. 이행 체크는 EVENT/EDIT 로 판정하므로
        # 여기가 VIEW 여도 관제요원은 체크리스트를 찍을 수 있다.
        SOP: {Action.VIEW},
        # 교대는 상황실 근무자끼리 하는 일이다. 작성(EDIT)과 인수 확인
        # (APPROVE)을 둘 다 준다 — OPR 에 APPROVE 를 주는 유일한 자원이다.
        HANDOVER: {Action.VIEW, Action.EDIT, Action.APPROVE},
        # 열람만. 자기가 처리한 이벤트의 근거는 봐야 하지만, 지우는 것은
        # 되돌릴 수 없어 부서담당자 이상으로 둔다.
        EVIDENCE: {Action.VIEW},
    },
}


def can(role: Role | str, resource: str, action: Action | str,
        *, domain: Domain | str | tuple | list | set | None = None,
        user_domains: set[str] | None = None) -> bool:
    """권한 판정. 도메인 종속 자원은 담당 도메인까지 확인한다.

    ``domain`` 에 여러 개를 주면 **그중 하나라도 담당이면 통과**한다.

    ★ 왜 「여러 개」가 필요한가 (2026-08-21)
        침수·교통을 나눈 뒤에도 ``/api/risk`` 처럼 **두 도메인의 값을 한
        응답에 함께 실어 보내는** 엔드포인트가 남는다. 여기에 도메인을
        하나만 걸면 교통만 담당하는 사용자가 자기 데이터를 못 보고,
        그렇다고 ``None`` 으로 열면 **인파만 담당하는 사용자에게까지**
        열린다 — 권한이 느슨해진다. 그래서 「둘 중 하나」를 표현한다.
    """
    role = Role(role)
    action = Action(action)
    allowed = PERMISSIONS.get(role, {}).get(resource, set())
    if action not in allowed:
        return False
    if role in DOMAIN_SCOPED_ROLES and domain is not None and resource in DOMAIN_SCOPED:
        doms = domain if isinstance(domain, (tuple, list, set, frozenset)) else (domain,)
        mine = user_domains or set()
        return any(Domain(d).value in mine for d in doms)
    return True


# --- 주민 알림 발송 정책 -----------------------------------------------------
# 설계서 4절의 미확정 항목. 기본값은 2인 승인(OPR 요청 -> MGR 승인)이며,
# 위험등급 「심각」에 한해 OPR 단독 발송을 허용하는 대안을 설정으로 열어 둔다.
# 어느 쪽이 맞는지는 지자체 재난 대응 운영규정에 달려 있어 우리가 정할 수 없다.
CRITICAL_LEVELS = {"심각", "critical", "CRITICAL"}


def solo_send_on_critical_enabled() -> bool:
    return os.environ.get("URBANGUARD_SOLO_SEND_ON_CRITICAL", "0") == "1"


def can_send_without_approval(role: Role | str, risk_level: str | None) -> bool:
    """승인 없이 즉시 발송할 수 있는가.

    MGR/SYS는 스스로 승인 권한이 있으므로 항상 가능하다. OPR은 설정이 켜져 있고
    위험등급이 「심각」일 때만 가능하며, 이 경우에도 감사로그에 사후 승인 필요로
    남는다(:mod:`.audit`).
    """
    role = Role(role)
    if role in (Role.SYS, Role.MGR):
        return True
    return solo_send_on_critical_enabled() and (risk_level or "") in CRITICAL_LEVELS
