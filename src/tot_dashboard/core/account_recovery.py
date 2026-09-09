"""계정 복구 — 비밀번호 초기화와 잠금 해제 (S-90 화면 · CLI 공용).

왜 별도 모듈인가
    비밀번호 초기화 경로가 두 군데 있습니다. 화면(S-90)에서 시스템관리자가 남의
    계정을 초기화하는 것과, **아무도 로그인할 수 없을 때** 서버에서 명령으로
    푸는 것입니다. 뒤엣것이 없으면 시스템관리자 계정의 비밀번호를 잃는 순간
    복구 방법이 없어 DB를 직접 손대야 합니다.

    두 경로가 같은 일을 서로 다르게 하면 위험합니다 — 한쪽만 실패 횟수를
    지운다든가, 한쪽만 감사 로그를 남기지 않는다든가. 그래서 실제 동작은 이
    모듈 하나에 두고 화면과 CLI 가 함께 씁니다.

보안 경계
    CLI 는 **서버에 접근할 수 있는 사람**만 쓸 수 있다는 것이 유일한 방벽입니다.
    이는 의도된 설계입니다 — 아무도 로그인할 수 없는 상황을 푸는 도구이므로
    로그인을 요구할 수 없고, 그렇다면 물리적·계정적 서버 접근 권한 말고는
    기댈 것이 없습니다. 그래서 CLI 사용도 **감사 로그에 남깁니다.**

기존 세션은 어떻게 되는가
    초기화하면 :attr:`~.models.User.must_change_password` 가 켜집니다. 가드는
    이 상태에서 비밀번호 변경 화면과 로그아웃 외의 모든 경로를 막으므로,
    **살아 있던 세션은 사실상 무력화됩니다.** 토큰 자체를 무효화하지는 않지만
    그 토큰으로 할 수 있는 일이 남지 않습니다.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .bootstrap import generate_password
from .db import get_session
from .models import User
from .roles import Role
from .security import hash_password, password_problem

# 감사 로그에 남기는 실행 주체. 화면에서 한 것과 구분돼야 한다 —
# 「서버에 직접 접근해서 풀었다」는 사실 자체가 감사 대상이다.
CLI_ACTOR = "(서버 CLI)"
CLI_IP = "server-local"

# 잠금 해제는 초기화와 다른 행위다. 비밀번호를 바꾸지 않고 실패 횟수만 지운다.
USER_UNLOCK = "user.unlock"


@dataclass(frozen=True)
class ResetResult:
    login_id: str
    name: str
    role: str
    password: str
    was_locked: bool
    failed_count: int


def find(db: Session, login_id: str) -> User | None:
    return db.scalar(select(User).where(User.login_id == (login_id or "").strip()))


def admin_accounts(db: Session) -> list[User]:
    """시스템관리자 계정 목록.

    복구 상황에서 「아이디가 뭐였지」를 먼저 풀어야 하는 경우가 많다.
    비밀번호는 해시조차 내보내지 않는다.
    """
    return list(db.scalars(
        select(User).where(User.role == Role.SYS.value).order_by(User.login_id)))


def reset_password(db: Session, login_id: str, *, password: str | None = None,
                   actor: str = CLI_ACTOR, ip: str = CLI_IP,
                   actor_user: User | None = None) -> ResetResult:
    """비밀번호를 초기화하고 잠금·실패 횟수를 함께 지운다.

    ``password`` 를 주지 않으면 임시 비밀번호를 만들어 돌려준다. 어느 쪽이든
    ``must_change_password`` 가 켜지므로, 초기화한 사람이 아는 비밀번호로는
    비밀번호 변경 말고 아무것도 할 수 없다.

    잠금을 함께 푸는 이유 — 비밀번호를 새로 줬는데 계정이 잠겨 있으면 여전히
    못 들어옵니다. 두 번 조작하게 만들 이유가 없습니다.
    """
    user = find(db, login_id)
    if user is None:
        raise LookupError(f"계정을 찾을 수 없습니다: {login_id}")
    if not user.is_active:
        raise ValueError(f"비활성 계정입니다: {login_id}. "
                         "먼저 계정을 활성화한 뒤 초기화하십시오.")

    if password is None:
        password = generate_password()
    else:
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)

    was_locked = user.locked_until is not None
    failed = user.failed_count

    user.pw_hash = hash_password(password)
    user.pw_updated_at = datetime.now(timezone.utc)
    user.failed_count = 0
    user.locked_until = None
    user.must_change_password = True

    audit.record(db, action=audit.USER_PASSWORD_RESET, user=actor_user,
                 login_id=actor if actor_user is None else "",
                 ip=ip, target=user.login_id,
                 before={"was_locked": was_locked, "failed_count": failed},
                 after={"must_change_password": True})
    db.commit()
    return ResetResult(login_id=user.login_id, name=user.name, role=user.role,
                       password=password, was_locked=was_locked,
                       failed_count=failed)


def unlock(db: Session, login_id: str, *, actor: str = CLI_ACTOR, ip: str = CLI_IP,
           actor_user: User | None = None) -> bool:
    """잠금과 실패 횟수만 지운다. **비밀번호는 그대로 둔다.**

    비밀번호를 기억하는 사람이 오타로 잠긴 경우가 대부분이라, 초기화까지 하면
    멀쩡한 비밀번호를 괜히 버리게 된다. 잠긴 적이 없으면 False 를 돌려준다.
    """
    user = find(db, login_id)
    if user is None:
        raise LookupError(f"계정을 찾을 수 없습니다: {login_id}")

    was_locked = user.locked_until is not None or user.failed_count > 0
    user.failed_count = 0
    user.locked_until = None
    audit.record(db, action=USER_UNLOCK, user=actor_user,
                 login_id=actor if actor_user is None else "",
                 ip=ip, target=user.login_id,
                 before={"locked": was_locked})
    db.commit()
    return was_locked


# --- 서버 CLI ---------------------------------------------------------------
#
#   urbanguard-passwd --list                    시스템관리자 계정 확인
#   urbanguard-passwd --login-id admin          비밀번호 초기화
#   urbanguard-passwd --login-id admin --unlock 잠금만 해제
#
# 비밀번호를 인자로 받지 않는다. PowerShell·bash 의 명령 이력에 평문으로 남기
# 때문이다(:mod:`.bootstrap` 과 같은 이유). 직접 정해야 하면 ``--from-stdin``
# 으로 표준입력에서 읽는다 — 이력에 남지 않는다.

def _console_utf8() -> None:
    """한글 출력이 cp949 콘솔에서 깨지거나 죽지 않게 한다.

    과거 감시 스크립트가 로그 한 줄 때문에 UnicodeEncodeError 로 죽은 적이 있다.
    복구 도구가 그것 때문에 실패하면 곤란하다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        if not sys.stdin or not sys.stdin.isatty():
            raise EOFError
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        # 대화할 수 없는 환경(스크립트·CI·일부 콘솔 래퍼)에서는 조용히 진행하지
        # 않는다. isatty() 만으로는 부족하다 — 실제로 입력을 시도해야 드러나는
        # 경우가 있어(래퍼 exe 로 실행할 때) 예외까지 함께 잡는다.
        print("\n[중단] 확인을 받을 수 없습니다. 자동 실행하려면 --yes 를 붙이십시오.",
              file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    ap = argparse.ArgumentParser(
        prog="urbanguard-passwd",
        description="UrbanGuard 계정 비밀번호 초기화·잠금 해제 "
                    "(서버에서 실행. 아무도 로그인할 수 없을 때 쓰는 복구 도구)")
    ap.add_argument("--login-id", help="대상 계정 아이디")
    ap.add_argument("--list", action="store_true",
                    help="시스템관리자 계정 목록을 보여 줍니다(변경 없음).")
    ap.add_argument("--unlock", action="store_true",
                    help="비밀번호는 그대로 두고 잠금·실패 횟수만 지웁니다.")
    ap.add_argument("--from-stdin", action="store_true",
                    help="새 비밀번호를 표준입력에서 읽습니다(명령 이력에 남지 않음).")
    ap.add_argument("--yes", action="store_true", help="확인 질문을 건너뜁니다.")
    args = ap.parse_args(argv)

    if not args.list and not args.login_id:
        ap.error("--login-id 또는 --list 중 하나가 필요합니다.")

    try:
        db = get_session()
    except Exception as e:  # noqa: BLE001
        print(f"[오류] 데이터베이스에 연결하지 못했습니다: {str(e)[:200]}",
              file=sys.stderr)
        print("      PostgreSQL 이 떠 있는지, URBANGUARD_DATABASE_URL 이 맞는지 "
              "확인하십시오.", file=sys.stderr)
        return 2

    try:
        if args.list:
            rows = admin_accounts(db)
            if not rows:
                print("시스템관리자 계정이 없습니다. "
                      "urbanguard-bootstrap 으로 먼저 만드십시오.")
                return 1
            print(f"시스템관리자 계정 {len(rows)}건")
            for u in rows:
                state = []
                if not u.is_active:
                    state.append("비활성")
                if u.locked_until is not None:
                    state.append("잠김")
                if u.must_change_password:
                    state.append("임시 비밀번호")
                if u.failed_count:
                    state.append(f"실패 {u.failed_count}회")
                last = (u.last_login_at.strftime("%Y-%m-%d %H:%M")
                        if u.last_login_at else "기록 없음")
                print(f"  {u.login_id:<20} {u.name} ({u.dept or '부서 미지정'})"
                      f"  최근 로그인 {last}"
                      f"{'  [' + ', '.join(state) + ']' if state else ''}")
            return 0

        if args.unlock:
            if not _confirm(f"{args.login_id} 계정의 잠금을 해제합니다. 진행할까요?",
                            args.yes):
                return 1
            was = unlock(db, args.login_id)
            print(f"{args.login_id} 계정의 잠금을 해제했습니다."
                  if was else
                  f"{args.login_id} 계정은 잠겨 있지 않았습니다(변경 없음).")
            return 0

        password = None
        if args.from_stdin:
            password = sys.stdin.readline().rstrip("\n")
            if not password:
                print("[오류] 표준입력에서 비밀번호를 읽지 못했습니다.", file=sys.stderr)
                return 1

        if not _confirm(f"{args.login_id} 계정의 비밀번호를 초기화합니다. "
                        "기존 비밀번호는 즉시 사용할 수 없게 됩니다. 진행할까요?",
                        args.yes):
            return 1

        result = reset_password(db, args.login_id, password=password)
    except LookupError as e:
        print(f"[오류] {e}", file=sys.stderr)
        print("      --list 로 등록된 시스템관리자 계정을 확인할 수 있습니다.",
              file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print("비밀번호를 초기화했습니다.")
    print(f"  아이디 : {result.login_id}  ({result.name} · {result.role})")
    if result.was_locked or result.failed_count:
        print(f"  ※ 잠겨 있던 계정이었습니다(실패 {result.failed_count}회). "
              "잠금도 함께 해제했습니다.")
    if password is None:
        print(f"  임시 비밀번호 : {result.password}")
        print("  ※ 이 값은 다시 표시되지 않습니다.")
    print("  ※ 로그인하면 비밀번호 변경 화면이 먼저 나오며, 바꾸기 전까지 다른 "
          "화면을 쓸 수 없습니다.")
    print("  ※ 이 초기화는 감사 로그(S-91)에 「서버 CLI」 로 기록됐습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
