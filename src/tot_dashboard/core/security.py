"""비밀번호 해시와 세션 쿠키.

passlib 대신 bcrypt 를 직접 쓴다. passlib 1.7 은 bcrypt 4.1+ 와 버전 탐지에서
충돌해 런타임 경고·예외를 내는 알려진 문제가 있고, 우리가 쓰는 기능은
hash/verify 둘뿐이라 래퍼가 주는 이득이 없다.

세션은 서명된 쿠키(itsdangerous)로 유지한다. 서버측 세션 테이블이 아니므로
**강제 로그아웃·동시접속 제한은 아직 불가능하다** — 공공기관 보안 요건에
포함될 경우 sessions 테이블 도입이 필요하다(docs/ui_design_spec.md 8절 참고).
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "urbanguard_session"
SESSION_MAX_AGE_SEC = int(os.environ.get("URBANGUARD_SESSION_MAX_AGE", "28800"))  # 8시간

# 설계서 5절 — 5회 실패 시 계정 잠금
MAX_FAILED = int(os.environ.get("URBANGUARD_MAX_FAILED_LOGIN", "5"))
LOCK_MINUTES = int(os.environ.get("URBANGUARD_LOCK_MINUTES", "10"))
MIN_PASSWORD_LEN = int(os.environ.get("URBANGUARD_MIN_PASSWORD_LEN", "9"))

_serializer: URLSafeTimedSerializer | None = None


def _secret() -> str:
    """세션 서명 키.

    운영에서는 반드시 .env 의 URBANGUARD_SECRET_KEY 로 고정해야 한다. 미설정 시
    프로세스마다 새 키가 생겨 재시작할 때마다 전원 로그아웃된다 — 개발 편의를
    위한 동작이지 운영용이 아니다.
    """
    key = os.environ.get("URBANGUARD_SECRET_KEY")
    if not key:
        key = globals().setdefault("_EPHEMERAL_KEY", secrets.token_urlsafe(48))
    return key


def serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_secret(), salt="urbanguard.session")
    return _serializer


def reset_serializer() -> None:
    global _serializer
    _serializer = None


# --- 비밀번호 ---------------------------------------------------------------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        # 해시 형식이 깨진 경우. 인증 실패로 처리하되 예외를 밖으로 내지 않는다.
        return False


def password_problem(plain: str) -> str | None:
    """비밀번호 정책 위반 사유. 통과하면 None.

    기관 보안지침에 맞춰 조정할 수 있도록 기준을 환경변수로 뺀다.
    """
    if len(plain) < MIN_PASSWORD_LEN:
        return f"비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다."
    kinds = sum([any(c.islower() for c in plain), any(c.isupper() for c in plain),
                 any(c.isdigit() for c in plain),
                 any(not c.isalnum() for c in plain)])
    if kinds < 3:
        return "영문 대문자·소문자·숫자·특수문자 중 3종류 이상을 포함해야 합니다."
    return None


# --- 세션 -------------------------------------------------------------------
def issue_session(user_id: int, login_id: str, role: str) -> str:
    return serializer().dumps({"uid": user_id, "lid": login_id, "role": role})


def read_session(token: str) -> dict | None:
    try:
        return serializer().loads(token, max_age=SESSION_MAX_AGE_SEC)
    except (BadSignature, SignatureExpired):
        return None


# --- 계정 잠금 --------------------------------------------------------------
def is_locked(locked_until: datetime | None) -> bool:
    if locked_until is None:
        return False
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > datetime.now(timezone.utc)


def lock_deadline() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)
