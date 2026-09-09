"""최초 관리자 계정 생성.

DB를 만든 직후에는 계정이 하나도 없어 로그인할 수 없다. 이 스크립트로 첫
시스템관리자를 만든 뒤, 나머지 계정은 화면(S-60)에서 발급한다.

실행::

    urbanguard-bootstrap --login-id admin --name 홍길동 --dept 정보통신과

비밀번호를 인자로 받지 않는 이유는 명령 이력(PowerShell/bash history)에
평문으로 남기 때문이다. 미지정 시 임시 비밀번호를 생성해 화면에 한 번만 출력한다.
"""
from __future__ import annotations

import argparse
import secrets
import sys

from .db import get_session
from .models import User, UserDomain
from .roles import Domain, Role
from .security import hash_password, password_problem


def generate_password() -> str:
    # 정책(9자 이상, 3종류 이상)을 확실히 만족시키기 위해 종류별로 섞는다.
    alphabet = "abcdefghijkmnopqrstuvwxyz"
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    sym = "!@#$%^&*"
    raw = [secrets.choice(alphabet) for _ in range(5)]
    raw += [secrets.choice(upper) for _ in range(3)]
    raw += [secrets.choice(digits) for _ in range(3)]
    raw += [secrets.choice(sym)]
    secrets.SystemRandom().shuffle(raw)
    return "".join(raw)


def create_user(db, *, login_id: str, name: str, dept: str, role: str,
                password: str | None = None, pw_hash: str | None = None,
                domains: list[str] | None = None,
                must_change: bool = True) -> User:
    """계정 생성.

    ``must_change`` 기본값이 True 인 이유 — 발급된 임시 비밀번호는 발급자도
    알고 있으므로, 본인이 바꾸기 전까지 다른 화면을 쓸 수 없게 한다.
    테스트처럼 즉시 사용해야 하는 경우에만 False 로 만든다.

    ``pw_hash``(2026-09-01 신설, 가입 신청 승인 경로 전용) — 이미 검증·
    해시된 값이 있으면(``core/signup_requests.py`` 참고, 신청자가 신청
    시점에 직접 정한 비밀번호) 그대로 쓰고 ``password_problem()``/
    ``hash_password()``를 다시 거치지 않는다. 관리자가 계정을 직접
    만드는 기존 경로(``password=`` 전달)는 전혀 바뀌지 않는다 — 이
    인자를 안 주면(``None``) 예전과 100% 동일하게 동작한다.
    """
    if pw_hash is not None:
        final_hash = pw_hash
    else:
        problem = password_problem(password or "")
        if problem:
            raise ValueError(problem)
        final_hash = hash_password(password)
    user = User(login_id=login_id, name=name, dept=dept, role=role,
                pw_hash=final_hash, is_active=True,
                must_change_password=must_change)
    db.add(user)
    db.flush()
    for d in domains or []:
        db.add(UserDomain(user_id=user.id, domain=Domain(d).value))
    return user


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="UrbanGuard 최초 관리자 계정 생성")
    ap.add_argument("--login-id", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--dept", default="정보통신과")
    ap.add_argument("--password", default=None,
                    help="미지정 시 임시 비밀번호를 생성합니다(권장).")
    args = ap.parse_args(argv)

    password = args.password or generate_password()
    db = get_session()
    try:
        exists = db.query(User).filter(User.login_id == args.login_id).one_or_none()
        if exists is not None:
            print(f"[오류] 이미 존재하는 아이디입니다: {args.login_id}", file=sys.stderr)
            return 1
        user = create_user(db, login_id=args.login_id, name=args.name,
                           dept=args.dept, role=Role.SYS.value, password=password,
                           domains=[d.value for d in Domain])
        db.commit()
    except ValueError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print("시스템관리자 계정을 만들었습니다.")
    print(f"  아이디   : {user.login_id}")
    print(f"  이름     : {user.name} ({user.dept})")
    if args.password is None:
        print(f"  임시 비밀번호 : {password}")
        print("  ※ 이 값은 다시 표시되지 않습니다. 로그인 후 즉시 변경하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
