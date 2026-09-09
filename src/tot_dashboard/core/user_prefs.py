"""사용자별 화면 설정 — 사람마다 다른 값을 담는다.

**왜 있나.** 관제요원은 자리를 옮겨 앉는다. 설정을 브라우저에 두면 옆자리
PC 로 가는 순간 사라지고, 본인은 **껐다고 생각한 것이 켜져 있는** 상태가
된다. 멈춤 같은 설정에서 그것은 **없는 안전을 보는** 일이다.

기관 설정(:mod:`~.settings`)과 자리가 다르다 — 그쪽은 **기관**이 정하는 값
(기관명·임계값)이고, 여기는 **사람**이 정하는 값이다.

⚠️ **캐시를 두지 않는다.** 기관 설정은 모든 요청이 읽으니 캐시가 값어치가
있지만, 여기는 사람 수만큼 갈라져 캐시 무효화가 까다롭고 이득은 작다. 그리고
**틀린 값을 들고 있는 것이 조금 느린 것보다 훨씬 나쁘다.**
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import UserPref

# --- 키 ---------------------------------------------------------------------
# S-05 멀티뷰 동작. "1" 이면 갱신, "0" 이면 멈춤.
KEY_MULTIVIEW_RUNNING = "multiview.running"

# 값이 없을 때 무엇으로 볼 것인가.
#
# ★ 멀티뷰는 **켜짐**이 기본이다. 관제 화면이 아무 말 없이 멎어 있으면 안
#   된다. 「저장된 적 없음」을 「꺼짐」으로 읽으면 처음 쓰는 사람에게 빈 화면이
#   나가고, 그것을 고장으로 본다.
DEFAULTS = {
    KEY_MULTIVIEW_RUNNING: "1",
}


def get(db: Session, user_id: int, key: str) -> str:
    row = db.execute(
        select(UserPref).where(UserPref.user_id == user_id,
                               UserPref.key == key)).scalar_one_or_none()
    if row is None:
        return DEFAULTS.get(key, "")
    return row.value


def set_value(db: Session, user_id: int, key: str, value: str) -> str:
    """저장하고 저장된 값을 돌려준다.

    호출한 쪽이 「무엇이 저장됐는지」를 화면에 그대로 쓸 수 있게 한다 —
    보낸 값과 저장된 값이 다를 수 있는데(길이 자름 등) 그때 화면이 거짓을
    말하면 안 된다.
    """
    value = (value or "").strip()[:255]
    row = db.execute(
        select(UserPref).where(UserPref.user_id == user_id,
                               UserPref.key == key)).scalar_one_or_none()
    if row is None:
        row = UserPref(user_id=user_id, key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()
    return row.value


def flag(db: Session, user_id: int, key: str) -> bool:
    """켜짐/꺼짐 값을 참·거짓으로 읽는다.

    ⚠️ **"0" 만 꺼짐이다.** 알 수 없는 값이 들어와도 꺼지지 않게 한다 —
    값이 깨졌을 때 관제 화면이 멎는 쪽으로 기우는 것은 위험하다.
    """
    return get(db, user_id, key) != "0"


def set_flag(db: Session, user_id: int, key: str, on: bool) -> bool:
    set_value(db, user_id, key, "1" if on else "0")
    return on
