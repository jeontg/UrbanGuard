"""서비스 재기동 — 화면에서 프로세스를 다시 띄운다 (S-01).

**왜 필요한가.** ``.env`` 는 프로세스가 뜰 때 한 번만 읽는다. 기상청 API 키를
넣거나 바꾸면 재기동해야 반영되는데, 지금은 서버에 붙어 명령을 쳐야 한다.
관제센터 운영자에게 그걸 시킬 수는 없다.

**⚠️ 이 버튼은 이 제품에서 가장 파괴적인 조작이다.** 누르는 순간 침수·인파·
노면 **상시 탐지가 모두 멎고**, 집중 감시(S-44)도 끊긴다. 그래서 세 겹으로
막았다.

1. **시스템관리자만** 보인다 (``SETTINGS_SYS`` × ``EXECUTE``)
2. **되살릴 사람이 있을 때만** 눌린다 — 감시 기동(``scripts/serve.py``) 아래가
   아니면 버튼이 잠기고 이유가 뜬다
3. 확인창에 **무엇이 멎는지**를 숫자로 보여 준다

3번이 없으면 「그냥 새로고침 같은 것」으로 읽힌다.

**되살릴 사람이 없으면 재기동이 아니라 그냥 종료다.** 맨 uvicorn 으로 띄운
프로세스를 죽이면 아무도 다시 띄우지 않는다 — 밤사이 서비스가 내려가 있던
2026-08-12 사고가 정확히 그 모습이었다(``scripts/serve.py`` 머리말). 그래서
감시 여부를 확인하고, 아니면 아예 막는다.

**리눅스·윈도우 양쪽에 납품된다.** 「누가 지켜보는가」는 systemd·윈도우
서비스·감시 스크립트로 나뉘고, 확인 방법과 안내문도 달라진다. 그 판단은
:mod:`.server_profile` 이 맡고 이 모듈은 **죽는 일**만 한다. 죽는 방식은
어느 쪽이든 같다 — 종료 코드 42.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

# 감시 기동(scripts/serve.py)이 자식에게 심어 주는 표시. 이 값이 없으면
# 「죽어도 되살릴 사람이 없다」는 뜻이다.
SUPERVISED_ENV = "URBANGUARD_SUPERVISED"

# 운영자가 화면에서 요청한 재기동임을 감시 프로세스에 알리는 종료 코드.
#
# **0 을 쓰면 안 된다** — serve.py 는 0 을 「정상 종료」로 보고 되살리지 않는다.
# 아무 비정상 코드나 쓰면 세그폴트와 구분이 안 돼, 백오프가 붙고 연속 실패
# 횟수에 잡혀 결국 「연속 20회 재기동 실패」로 멈춘다.
RESTART_EXIT_CODE = 42

# 응답을 다 보내고 나서 죽어야 한다. 바로 죽으면 브라우저는 「연결 끊김」만
# 보고, 운영자는 눌렀는데 무슨 일이 일어났는지 알 수 없다.
RESTART_DELAY_SEC = 1.5

_started_at = time.monotonic()
_started_wall = datetime.now(timezone.utc)

# 재기동이 이미 예약됐는지. 두 번 눌러 두 번 죽이는 일을 막는다.
_pending = threading.Event()


def is_supervised() -> bool:
    return os.environ.get(SUPERVISED_ENV, "") == "1"


def uptime_seconds() -> float:
    return time.monotonic() - _started_at


def started_at() -> datetime:
    return _started_wall


def uptime_text() -> str:
    s = int(uptime_seconds())
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분"
    if s < 86400:
        return f"{s // 3600}시간 {(s % 3600) // 60}분"
    return f"{s // 86400}일 {(s % 86400) // 3600}시간"


def restart_pending() -> bool:
    return _pending.is_set()


def can_restart(prof: dict | None = None) -> tuple[bool, str]:
    """(가능 여부, 이유). 이유는 **불가능할 때 화면에 그대로 띄운다.**

    막는 이유를 안 적으면 「버튼이 왜 회색이지」로 끝나고, 운영자는 결국
    서버에 직접 붙는다.

    ``prof`` 는 :func:`~.server_profile.profile` 결과다. 주지 않으면 설정을
    읽지 못하는 상황(DB 미가동 등)으로 보고 **감시 스크립트만** 인정한다 —
    설정을 못 읽었다고 열어 주면 안 된다.
    """
    if _pending.is_set():
        return False, "이미 재기동이 예약돼 있습니다. 잠시 기다려 주십시오."
    if prof is not None:
        return bool(prof["can_restart"]), prof["reason"]
    if not is_supervised():
        return False, ("감시 기동으로 떠 있지 않아 재기동할 수 없습니다. "
                       "지금 프로세스를 내리면 되살릴 것이 없습니다. "
                       "설정 → 서버 운영(S-87)에서 등록 방법을 확인하십시오.")
    return True, ""


def request_restart(shutdown, *, delay: float = RESTART_DELAY_SEC) -> bool:
    """재기동을 예약한다. 이미 예약돼 있으면 ``False``.

    ``shutdown`` 은 워커 스레드를 정리하는 함수다. 정리하지 않고 죽이면
    스트림 연결과 임시 파일이 남는다.

    **왜 ``os._exit`` 인가** — 정상 종료 경로로 내려가면 uvicorn 이 종료 코드
    0 을 돌려주고, 그러면 감시 프로세스가 「정상 종료」로 보고 되살리지
    않는다. 우리가 원하는 것은 **다시 뜨는 것**이므로 종료 코드를 직접
    정해야 한다. 워커 정리는 아래에서 손으로 해 준다.
    """
    if not _pending.is_set():
        _pending.set()
    else:
        return False

    def _worker() -> None:
        # 응답이 브라우저에 닿을 시간을 준다.
        time.sleep(max(delay, 0.1))
        try:
            shutdown()
        except Exception as e:  # noqa: BLE001
            # 정리에 실패해도 재기동은 해야 한다 — 정리하려다 못 죽으면
            # 「눌렀는데 아무 일도 안 일어난다」가 된다.
            print(f"[service] 종료 정리 중 오류(무시하고 재기동): {str(e)[:160]}")
        print(f"[service] 운영자 요청으로 재기동합니다 (exit {RESTART_EXIT_CODE}).")
        # flush 를 못 하면 위 줄이 로그에 안 남는다.
        try:
            import sys
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(RESTART_EXIT_CODE)

    threading.Thread(target=_worker, name="urbanguard-restart",
                     daemon=True).start()
    return True


def state(*, watchers: int = 0, prof: dict | None = None) -> dict:
    """화면 카드와 재기동 후 상태 확인(polling)이 함께 쓰는 값."""
    ok, why = can_restart(prof)
    return {
        "supervised": is_supervised(),
        "profile": prof,
        "uptime_sec": int(uptime_seconds()),
        "uptime_text": uptime_text(),
        "started_at": _started_wall.isoformat(),
        "watchers": watchers,
        "can_restart": ok,
        "reason": why,
        "pending": _pending.is_set(),
    }
