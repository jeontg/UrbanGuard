"""서버 운영 환경 — 운영체제와 재기동 방식 (S-87).

**왜 필요한가.** 이 시스템은 리눅스와 윈도우 양쪽에 납품된다. 그런데 재기동은
운영체제마다 다르게 동작한다 — 정확히 말하면 **운영체제가 아니라 「누가
프로세스를 지켜보는가」가 다르다.**

⚠️ **OS만으로는 재기동 방식이 정해지지 않는다.** 같은 리눅스라도 systemd 로
띄웠는지 도커로 띄웠는지에 따라 다르고, 윈도우도 서비스로 등록했는지 콘솔에서
띄웠는지에 따라 다르다. 그래서 이 모듈은 **운영체제와 감시 방식을 따로**
받는다. OS 는 「어떤 방식을 고를 수 있는가」와 안내문·로그 위치를 정하고,
실제 동작은 감시 방식이 정한다.

**재기동 동작 자체는 모든 방식에서 같다** — :data:`~.service_control.
RESTART_EXIT_CODE` 로 죽고, 지켜보던 쪽이 다시 띄운다.

* systemd — ``Restart=always`` 가 0이 아닌 종료를 되살린다
* NSSM(윈도우 서비스) — 기본 동작이 종료 시 재시작이다
* serve.py — 코드 42 를 「운영자 요청」으로 보고 백오프 없이 되살린다

다른 것은 **「지켜보는 쪽이 정말 있는가」를 어떻게 확인하는가**와, 안 될 때
**어디를 보라고 안내하는가**이다. 이 둘이 이 모듈의 알맹이다.

`systemctl restart` 같은 **외부 명령을 우리가 실행하지 않는 이유** — 자기
자신을 죽이는 명령이라 중간에 프로세스가 사라져 결과를 알 수 없고, 권한
상승(sudo·polkit)이 필요해 관제 서비스 계정에 그 권한을 주게 된다. 종료 코드로
넘기면 추가 권한이 필요 없다.
"""
from __future__ import annotations

import os
import sys

# --- 운영체제 ---------------------------------------------------------------
OS_AUTO = "auto"
OS_LINUX = "linux"
OS_WINDOWS = "windows"

OS_LABELS = {
    OS_AUTO: "자동 감지",
    OS_LINUX: "리눅스",
    OS_WINDOWS: "윈도우",
}


def detect_os() -> str:
    """지금 **실제로** 돌고 있는 운영체제."""
    return OS_WINDOWS if sys.platform.startswith("win") else OS_LINUX


# --- 감시(재기동) 방식 -------------------------------------------------------
ST_SYSTEMD = "systemd"        # 리눅스 표준
ST_NSSM = "nssm"              # 윈도우 서비스 (NSSM 등)
ST_SUPERVISOR = "supervisor"  # scripts/serve.py — 양쪽 공통, 개발·실증용
ST_NONE = "none"              # 재기동 불가

# 운영체제별로 **고를 수 있는** 방식. 리눅스에 윈도우 서비스를 고르는 것 같은
# 조합을 화면에서 아예 못 만들게 한다.
STRATEGIES_BY_OS = {
    OS_LINUX: (ST_SYSTEMD, ST_SUPERVISOR, ST_NONE),
    OS_WINDOWS: (ST_NSSM, ST_SUPERVISOR, ST_NONE),
}

STRATEGY_LABELS = {
    ST_SYSTEMD: "systemd 서비스",
    ST_NSSM: "윈도우 서비스 (NSSM)",
    ST_SUPERVISOR: "감시 스크립트 (scripts/serve.py)",
    ST_NONE: "없음 — 재기동 불가",
}

# systemd 가 서비스 프로세스에 넣어 주는 환경변수. **이것이 있으면 systemd
# 아래에서 돌고 있다는 확실한 증거**다(systemd 232+).
SYSTEMD_ENV = "INVOCATION_ID"

# 윈도우 서비스에는 이런 표준 표시가 없다. 서비스 등록 시 이 환경변수를 넣도록
# 안내하고, 없으면 「확인 불가」로 정직하게 표시한다 — 있지도 않은 근거로
# 「감시 중」이라고 하면 안 된다.
WINDOWS_ENV = "URBANGUARD_WIN_SERVICE"


def detect_strategy() -> str | None:
    """지금 **증명할 수 있는** 감시 방식. 확인 못 하면 ``None``.

    ``None`` 은 「감시가 없다」가 아니라 **「모르겠다」**이다. 둘을 섞으면
    화면이 거짓말을 하게 된다.
    """
    from .service_control import SUPERVISED_ENV
    if os.environ.get(SYSTEMD_ENV):
        return ST_SYSTEMD
    if os.environ.get(WINDOWS_ENV) == "1":
        return ST_NSSM
    if os.environ.get(SUPERVISED_ENV) == "1":
        return ST_SUPERVISOR
    return None


# --- 방식별 안내 -------------------------------------------------------------
# 재기동이 안 될 때 **어디를 봐야 하는가**. 이것이 없으면 운영자는 서버에 붙어
# 헤매게 되고, 그럴 바에는 버튼이 없는 편이 낫다.
GUIDE = {
    ST_SYSTEMD: {
        "setup": (
            "유닛 파일에 다음이 있어야 합니다.\n"
            "  [Service]\n"
            "  Restart=always\n"
            "  RestartSec=3\n"
            "재기동은 종료 코드 42 로 이뤄지므로 RestartPreventExitStatus 에 "
            "42 를 넣으면 안 됩니다."),
        "log": "journalctl -u {name} -n 100 --no-pager",
        "status": "systemctl status {name}",
        "detect_note": "systemd 가 넣어 주는 INVOCATION_ID 로 확인합니다.",
    },
    ST_NSSM: {
        "setup": (
            "NSSM 기본 설정은 종료 시 재시작이므로 그대로 두면 됩니다.\n"
            "  nssm set {name} AppExit Default Restart\n"
            "확인용 표시를 넣어 주십시오 — 없으면 화면이 「감시 여부 확인 "
            "불가」로 표시합니다.\n"
            "  nssm set {name} AppEnvironmentExtra URBANGUARD_WIN_SERVICE=1"),
        "log": "이벤트 뷰어 → Windows 로그 → 응용 프로그램 (원본: {name})",
        "status": "sc query {name}",
        "detect_note": ("윈도우 서비스에는 표준 표시가 없어, 등록 시 넣은 "
                        "URBANGUARD_WIN_SERVICE=1 로만 확인합니다."),
    },
    ST_SUPERVISOR: {
        "setup": (
            "다음으로 띄우면 됩니다.\n"
            "  python scripts/serve.py --host 127.0.0.1 --port 8033\n"
            "⚠️ 임시 방편입니다 — 서버가 재부팅되면 함께 죽습니다. 운영 "
            "납품에서는 systemd 나 윈도우 서비스로 등록하십시오."),
        "log": "data/logs/serve.log",
        "status": "(감시 스크립트 콘솔)",
        "detect_note": "serve.py 가 넣어 주는 URBANGUARD_SUPERVISED 로 확인합니다.",
    },
    ST_NONE: {
        "setup": ("재기동을 쓰려면 위 방식 중 하나로 등록해야 합니다. "
                  "지금은 프로세스를 내리면 되살릴 것이 없습니다."),
        "log": "(없음)",
        "status": "(없음)",
        "detect_note": "재기동 기능을 쓰지 않는 설정입니다.",
    },
}


# **표시가 없으면 감시도 없다**고 단정할 수 있는 방식.
#
# systemd 는 서비스로 띄우면 INVOCATION_ID 를 반드시 넣고, serve.py 는 자식에게
# 반드시 표시를 심는다. 따라서 이 방식들에서 표시가 없다는 것은 「모르겠다」가
# 아니라 **「감시 아래가 아니다」**라는 확실한 결론이다.
VERIFIABLE = (ST_SYSTEMD, ST_SUPERVISOR)

# 프로그램이 감시 여부를 **스스로 확인할 수 없는** 방식. 이 경우에만 관리자
# 확인(체크박스)을 받는다. 윈도우 서비스에는 표준 표시가 없어, 우리가 안내하는
# 표시를 안 넣었을 뿐 실제로는 서비스로 돌고 있을 수 있다.
#
# systemd 처럼 확실한 증거가 있는 방식에까지 확인을 받으면, 확인이 형식이 되고
# 정작 필요한 곳에서 무뎌진다.
NEEDS_ACK = (ST_NSSM,)

# 서비스·유닛 이름이 의미 없는 방식.
NO_SERVICE_NAME = (ST_SUPERVISOR, ST_NONE)


def needs_ack(strategy: str) -> bool:
    return strategy in NEEDS_ACK


def default_service_name(strategy: str) -> str:
    return {ST_SYSTEMD: "urbanguard", ST_NSSM: "UrbanGuard"}.get(strategy, "")


def normalize(os_choice: str, strategy: str) -> tuple[str, str]:
    """설정값을 실제로 쓸 수 있는 조합으로 맞춘다.

    ``auto`` 는 지금 OS 로 바꾸고, 그 OS 에 없는 방식이면 **재기동 불가**로
    떨어뜨린다 — 리눅스에서 윈도우 서비스로 재기동하려다 조용히 죽는 것보다
    「불가」라고 말하는 편이 낫다.
    """
    eff_os = detect_os() if os_choice not in (OS_LINUX, OS_WINDOWS) else os_choice
    allowed = STRATEGIES_BY_OS[eff_os]
    return eff_os, (strategy if strategy in allowed else ST_NONE)


def guide_for(strategy: str, service_name: str = "") -> dict:
    """방식별 안내문. ``{name}`` 자리에 서비스·유닛 이름을 넣는다."""
    g = GUIDE.get(strategy, GUIDE[ST_NONE])
    name = service_name or default_service_name(strategy) or "urbanguard"
    return {k: v.replace("{name}", name) for k, v in g.items()}


def profile(*, os_choice: str, strategy: str, service_name: str = "",
            ack: bool = False) -> dict:
    """설정과 실제를 함께 담은 현황.

    **configured 와 detected 를 둘 다 돌려준다.** 하나만 보여 주면 설정이
    현실과 어긋나 있어도 화면은 멀쩡해 보인다.
    """
    eff_os, eff_strategy = normalize(os_choice, strategy)
    real_os = detect_os()
    detected = detect_strategy()

    # OS 불일치 — 리눅스 납품용으로 설정해 두고 윈도우에서 돌리는 상황.
    # 설정 자체는 틀리지 않았을 수 있어(납품 준비) 막지 않고 알리기만 한다.
    os_mismatch = (os_choice in (OS_LINUX, OS_WINDOWS) and os_choice != real_os)

    if eff_strategy == ST_NONE:
        ok, why = False, ("재기동 방식이 「없음」으로 설정돼 있습니다. "
                          "설정 → 서버 운영(S-87)에서 지정하십시오.")
    elif detected == eff_strategy:
        ok, why = True, ""
    elif detected is not None:
        # 설정과 실제가 서로 다른 방식을 가리킨다. 이건 진짜 위험하다 —
        # 예를 들어 systemd 로 설정했는데 실제로는 serve.py 아래일 때,
        # 안내문이 엉뚱한 곳(journalctl)을 가리키게 된다.
        ok, why = False, (
            f"설정은 「{STRATEGY_LABELS[eff_strategy]}」인데 실제로는 "
            f"「{STRATEGY_LABELS[detected]}」 아래에서 돌고 있습니다. "
            "설정을 실제와 맞춰 주십시오.")
    elif eff_strategy in VERIFIABLE:
        # 표시가 없으면 감시도 없다 — 「모르겠다」가 아니라 확실한 결론이다.
        # 여기서 관리자 확인으로 열어 주면 안 된다. 우리가 아니라고 아는데
        # 사람 말을 믿고 서비스를 내리는 꼴이 된다.
        ok, why = False, (
            f"「{STRATEGY_LABELS[eff_strategy]}」로 설정돼 있으나 실제로는 그 "
            "아래에서 돌고 있지 않습니다. 지금 프로세스를 내리면 되살릴 것이 "
            "없습니다. 설정 → 서버 운영(S-87)에서 등록 방법을 확인하십시오.")
    elif ack:
        # 확인할 방법이 없는 경우(윈도우 서비스). 관리자가 책임지고
        # 「감시 중임을 확인했다」고 표시한 경우에만 연다.
        ok, why = True, ""
    else:
        ok, why = False, (
            f"「{STRATEGY_LABELS[eff_strategy]}」로 설정돼 있으나 실제 감시 "
            "여부를 확인할 수 없습니다. 지금 프로세스를 내리면 되살아나지 "
            "않을 수 있습니다. 설정 → 서버 운영(S-87)에서 등록 방법을 "
            "확인하시거나, 확인하셨다면 「감시 확인」을 표시해 주십시오.")

    return {
        "os": eff_os, "os_label": OS_LABELS[eff_os],
        "os_choice": os_choice, "real_os": real_os,
        "real_os_label": OS_LABELS[real_os], "os_mismatch": os_mismatch,
        "strategy": eff_strategy,
        "strategy_label": STRATEGY_LABELS[eff_strategy],
        "detected": detected,
        "detected_label": (STRATEGY_LABELS[detected] if detected
                           else "확인 불가"),
        "service_name": service_name or default_service_name(eff_strategy),
        "ack": ack, "can_restart": ok, "reason": why,
        "guide": guide_for(eff_strategy, service_name),
    }
