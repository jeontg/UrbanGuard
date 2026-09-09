"""오류 기록·검색·삭제 (S-92).

세 가지 원칙으로 만들었습니다.

1. **오류 기록이 본래 업무를 막아서는 안 된다.** 기록 도중 난 예외는 모두
   삼키고 로깅만 합니다. DB가 죽어서 오류를 못 남기는 상황에서, 그 사실 때문에
   관제 화면까지 죽으면 안 됩니다(:mod:`.audit` 과 같은 원칙).
2. **같은 오류는 묶는다.** CCTV 재접속 실패는 초당 수십 번 납니다. 발생마다 행을
   만들면 중요한 오류 한 건이 그 속에 묻힙니다.
3. **기록하다 다시 오류를 내면 안 된다.** 기록 경로에서 난 예외를 다시 기록하면
   무한히 돕니다. 스레드별 재진입 표시로 막습니다.

삭제는 **실제 삭제**입니다(사용자 지정). 감사 로그와 달리 오류 이력은 지웁니다.
대신 지운 행위 자체는 감사 로그에 남깁니다 — 무엇을 몇 건 지웠는지가 남아야
「장애 기록을 지운 것 아니냐」는 물음에 답할 수 있습니다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import error_catalog as catalog
from .db import get_session
from .models import ErrorCode, ErrorLog

log = logging.getLogger("urbanguard.errors")

# 같은 지문의 오류를 한 행으로 합치는 시간 창. 이보다 오래된 건은 새 행이 된다 —
# 지난주 장애와 오늘 장애가 한 줄로 합쳐지면 「언제부터」가 사라진다.
AGGREGATE_WINDOW = timedelta(hours=24)

# 화면 한 번에 읽는 최대 행 수.
DEFAULT_LIMIT = 200

MESSAGE_MAX = 2000
DETAIL_MAX = 8000

# 기록 경로에서 난 예외를 다시 기록하지 않기 위한 스레드별 표시.
_local = threading.local()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- 민감정보 가리기 ---------------------------------------------------------
# 예외 메시지에는 접속 문자열이 통째로 실려 오는 일이 흔하다. 오류 화면은
# 시스템관리자만 보지만, 화면에 찍힌 것은 캡처되어 메일로 돌아다닌다.
_REDACT = (
    # postgresql+psycopg://user:PASSWORD@host  →  비밀번호만 가린다
    (re.compile(r"(://[^:/\s]+:)([^@/\s]+)(@)"), r"\1***\3"),
    (re.compile(r"((?:password|passwd|pwd)\s*[=:]\s*)(\S+)", re.I), r"\1***"),
    (re.compile(r"((?:api[_-]?key|token|secret)\s*[=:]\s*)(\S+)", re.I), r"\1***"),
)


def redact(text: str) -> str:
    """비밀번호·API 키를 가린다. 원문을 알아볼 수는 있게 형태는 남긴다."""
    for pat, repl in _REDACT:
        text = pat.sub(repl, text)
    return text


# --- 지문 만들기 -------------------------------------------------------------
# 같은 오류인지 판정하는 열쇠. 메시지에서 매번 달라지는 부분(숫자·시각·주소·id)을
# 지우고 남은 뼈대로 해시를 만든다. 이걸 안 하면 「카메라 3 접속 실패」와
# 「카메라 7 접속 실패」가 다른 오류로 잡혀 목록이 다시 폭주한다.
_VARIABLE = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?\b"),   # 시각
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
               r"[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),                 # UUID
    re.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b"),                 # IP[:포트]
    re.compile(r"0x[0-9a-f]+", re.I),                                # 메모리 주소
    re.compile(r"\d+"),                                              # 남은 숫자
)


def _skeleton(text: str) -> str:
    for pat in _VARIABLE:
        text = pat.sub("#", text)
    return " ".join(text.split())[:400]


def fingerprint(code: str, path: str, message: str) -> str:
    raw = f"{code}|{_skeleton(path)}|{_skeleton(message)}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


# --- 기록 -------------------------------------------------------------------
def record(*, code: str = "", message: str = "", detail: str = "",
           exc: BaseException | None = None, source: str = "web",
           path: str = "", method: str = "", status_code: int | None = None,
           login_id: str = "", ip: str = "", severity: str = "",
           db: Session | None = None) -> int | None:
    """오류 1건을 남긴다. 남긴(또는 합친) 행의 id, 실패하면 None.

    ``code`` 를 주지 않으면 :func:`~.error_catalog.classify` 가 정합니다.
    ``db`` 를 주지 않으면 이 함수가 세션을 열고 닫습니다 — 백그라운드 스레드에는
    요청 세션이 없기 때문입니다.
    """
    if getattr(_local, "busy", False):
        # 기록 도중 난 오류. 여기서 또 기록하면 무한히 돈다.
        return None
    _local.busy = True
    try:
        return _record(code=code, message=message, detail=detail, exc=exc,
                       source=source, path=path, method=method,
                       status_code=status_code, login_id=login_id, ip=ip,
                       severity=severity, db=db)
    except Exception:  # noqa: BLE001
        # 오류 기록 실패로 재난 대응 업무가 멈추면 안 된다.
        log.exception("오류 기록 실패: code=%s path=%s", code, path)
        return None
    finally:
        _local.busy = False


def _record(*, code, message, detail, exc, source, path, method, status_code,
            login_id, ip, severity, db) -> int | None:
    if not message and exc is not None:
        message = f"{type(exc).__name__}: {exc}"
    code = code or catalog.classify(exc, message=message,
                                    status_code=status_code, path=path)
    message = redact(message or "")[:MESSAGE_MAX]
    detail = redact(detail or "")[:DETAIL_MAX]
    fp = fingerprint(code, path, message)

    own = db is None
    session = db if db is not None else get_session()
    try:
        if not severity:
            severity = _severity_of(session, code)
        now = _now()
        # 같은 지문의 **미처리** 최근 행이 있으면 거기에 합친다. 처리완료로
        # 표시한 건에 새 발생을 얹으면, 조치했는데 또 났다는 사실이 가려진다.
        row = session.scalars(
            select(ErrorLog)
            .where(ErrorLog.fingerprint == fp,
                   ErrorLog.resolved.is_(False),
                   ErrorLog.last_seen_at >= now - AGGREGATE_WINDOW)
            .order_by(ErrorLog.last_seen_at.desc(), ErrorLog.id.desc())
            .limit(1)
        ).first()

        if row is not None:
            row.count += 1
            row.last_seen_at = now
            # 마지막 발생의 맥락으로 갱신한다 — 조사할 때 보는 것은 최근 건이다.
            if detail:
                row.detail = detail
            if login_id:
                row.login_id = login_id
            if ip:
                row.ip = ip
        else:
            row = ErrorLog(
                code=code, fingerprint=fp, severity=severity, message=message,
                detail=detail, source=source or "web", path=path[:255],
                method=(method or "")[:8], status_code=status_code,
                login_id=login_id[:64], ip=ip[:64],
                count=1, first_seen_at=now, last_seen_at=now,
            )
            session.add(row)
        session.commit()
        return row.id
    finally:
        if own:
            session.close()


def _severity_of(db: Session, code: str) -> str:
    row = db.scalars(select(ErrorCode).where(ErrorCode.code == code)).first()
    if row is not None:
        return row.severity
    # 사전에 없는 코드. 기본값을 준다 — 사전 등록 여부가 심각도를 좌우하면 안 된다.
    for c in catalog.BUILTIN:
        if c["code"] == code:
            return c["severity"]
    return "error"


# --- 조회 -------------------------------------------------------------------
def search(db: Session, *, q: str = "", code: str = "", category: str = "",
           severity: str = "", source: str = "", resolved: str = "",
           days: int = 0, limit: int = DEFAULT_LIMIT) -> list[ErrorLog]:
    """발생 이력 검색. 조건은 모두 선택이며 AND 로 묶인다.

    ``resolved`` 는 ``""``(전체) / ``"0"``(미처리) / ``"1"``(처리완료).
    ``days`` 가 0이면 기간 제한이 없다.
    """
    stmt = select(ErrorLog).order_by(ErrorLog.last_seen_at.desc(), ErrorLog.id.desc()).limit(limit)
    if code:
        stmt = stmt.where(ErrorLog.code == code)
    if category:
        # 분류는 코드 문자열 안에 들어 있다 (UG-<분류>-nnn). 조인을 피해
        # 사전이 비어 있어도 검색이 되도록 한다.
        stmt = stmt.where(ErrorLog.code.like(f"UG-{category}-%"))
    if severity:
        stmt = stmt.where(ErrorLog.severity == severity)
    if source:
        stmt = stmt.where(ErrorLog.source == source)
    if resolved in ("0", "1"):
        stmt = stmt.where(ErrorLog.resolved.is_(resolved == "1"))
    if days > 0:
        stmt = stmt.where(ErrorLog.last_seen_at >= _now() - timedelta(days=days))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(ErrorLog.message.ilike(like)
                          | ErrorLog.path.ilike(like)
                          | ErrorLog.code.ilike(like)
                          | ErrorLog.detail.ilike(like))
    return list(db.scalars(stmt).all())


def get(db: Session, log_id: int) -> ErrorLog | None:
    return db.get(ErrorLog, log_id)


def summary(db: Session, *, days: int = 7) -> dict:
    """화면 상단 요약. 「지금 무엇을 먼저 봐야 하는가」에 답한다."""
    since = _now() - timedelta(days=days)
    base = select(func.count(ErrorLog.id)).where(ErrorLog.last_seen_at >= since)
    unresolved = db.scalar(base.where(ErrorLog.resolved.is_(False))) or 0
    critical = db.scalar(
        base.where(ErrorLog.resolved.is_(False),
                   ErrorLog.severity == "critical")) or 0
    occurrences = db.scalar(
        select(func.coalesce(func.sum(ErrorLog.count), 0))
        .where(ErrorLog.last_seen_at >= since)) or 0
    top = db.execute(
        select(ErrorLog.code, func.sum(ErrorLog.count).label("n"))
        .where(ErrorLog.last_seen_at >= since)
        .group_by(ErrorLog.code).order_by(func.sum(ErrorLog.count).desc())
        .limit(5)
    ).all()
    return {"days": days, "unresolved": int(unresolved),
            "critical": int(critical), "occurrences": int(occurrences),
            "top": [{"code": c, "count": int(n)} for c, n in top]}


# --- 조치·삭제 ---------------------------------------------------------------
def resolve(db: Session, log_id: int, *, by: str = "", note: str = "",
            undo: bool = False) -> bool:
    """처리완료로 표시(또는 해제)한다. 행은 지우지 않는다."""
    row = db.get(ErrorLog, log_id)
    if row is None:
        return False
    if undo:
        row.resolved = False
        row.resolved_at = None
        row.resolved_by = ""
    else:
        row.resolved = True
        row.resolved_at = _now()
        row.resolved_by = by[:64]
        row.resolve_note = (note or "")[:DETAIL_MAX]
    db.commit()
    return True


def delete(db: Session, log_ids: list[int]) -> int:
    """오류 이력을 **실제로 지운다.** 지운 건수를 돌려준다.

    되돌릴 수 없으므로, 호출부는 지운 사실을 감사 로그에 남겨야 한다
    (:mod:`.audit`). 이 함수는 감사 기록을 대신하지 않는다 — 누가 무엇을
    지웠는지는 호출 맥락에만 있기 때문이다.
    """
    ids = [int(i) for i in log_ids if i]
    if not ids:
        return 0
    n = db.execute(sa_delete(ErrorLog).where(ErrorLog.id.in_(ids))).rowcount or 0
    db.commit()
    return int(n)


def delete_by_filter(db: Session, *, code: str = "", resolved_only: bool = True,
                     before_days: int = 0) -> int:
    """조건에 맞는 이력을 한꺼번에 지운다.

    기본값이 ``resolved_only=True`` 인 이유 — 조건 삭제는 실수하면 크게
    지워집니다. 아직 조치하지 않은 오류까지 한 번에 날아가지 않도록,
    호출부가 명시적으로 끄지 않는 한 처리완료 건만 지웁니다.
    """
    stmt = sa_delete(ErrorLog)
    conds = []
    if resolved_only:
        conds.append(ErrorLog.resolved.is_(True))
    if code:
        conds.append(ErrorLog.code == code)
    if before_days > 0:
        conds.append(ErrorLog.last_seen_at < _now() - timedelta(days=before_days))
    if not conds:
        # 조건이 하나도 없으면 전체 삭제가 된다. 사고를 막기 위해 거부한다.
        raise ValueError("조건 없는 전체 삭제는 허용하지 않습니다.")
    n = db.execute(stmt.where(*conds)).rowcount or 0
    db.commit()
    return int(n)


# --- 오류 코드 사전 ----------------------------------------------------------
def seed_builtin(db: Session) -> int:
    """기본 코드를 심는다. **이미 있는 코드는 건드리지 않는다.**

    덮어쓰지 않는 이유 — 현장에서 알아낸 조치 방법을 담당자가 코드에 적어 두는데,
    배포할 때마다 초기값으로 되돌리면 그 지식이 매번 사라집니다.
    """
    have = set(db.scalars(select(ErrorCode.code)).all())
    added = 0
    for c in catalog.BUILTIN:
        if c["code"] in have:
            continue
        db.add(ErrorCode(code=c["code"], category=c["category"], title=c["title"],
                         severity=c["severity"], cause=c["cause"],
                         resolution=c["resolution"], builtin=True))
        added += 1
    if added:
        db.commit()
    return added


def codes(db: Session, *, q: str = "", category: str = "",
          include_inactive: bool = True) -> list[ErrorCode]:
    stmt = select(ErrorCode).order_by(ErrorCode.code)
    if category:
        stmt = stmt.where(ErrorCode.category == category)
    if not include_inactive:
        stmt = stmt.where(ErrorCode.is_active.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(ErrorCode.code.ilike(like) | ErrorCode.title.ilike(like)
                          | ErrorCode.cause.ilike(like)
                          | ErrorCode.resolution.ilike(like))
    return list(db.scalars(stmt).all())


def code_map(db: Session) -> dict[str, ErrorCode]:
    """코드 → 사전 항목. 이력 목록에 원인·해결방법을 붙일 때 쓴다."""
    return {c.code: c for c in db.scalars(select(ErrorCode)).all()}


def get_code(db: Session, code: str) -> ErrorCode | None:
    return db.scalars(select(ErrorCode).where(ErrorCode.code == code)).first()


CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z0-9]+){1,3}$")


def save_code(db: Session, *, code: str, category: str, title: str,
              severity: str, cause: str, resolution: str,
              is_active: bool = True, by: str = "") -> ErrorCode:
    """코드를 새로 만들거나 고친다. 코드 문자열 자체는 바꿀 수 없다."""
    code = (code or "").strip().upper()
    if not CODE_PATTERN.match(code):
        raise ValueError("코드 형식이 올바르지 않습니다. 예: UG-CCTV-006")
    if category not in catalog.CATEGORIES:
        raise ValueError(f"분류가 올바르지 않습니다: {category}")
    if severity not in catalog.SEVERITIES:
        raise ValueError(f"심각도가 올바르지 않습니다: {severity}")
    if not (title or "").strip():
        raise ValueError("오류명을 입력하십시오.")

    row = get_code(db, code)
    if row is None:
        row = ErrorCode(code=code, builtin=False)
        db.add(row)
    row.category = category
    row.title = title.strip()[:160]
    row.severity = severity
    row.cause = cause or ""
    row.resolution = resolution or ""
    row.is_active = is_active
    row.updated_by = by[:64]
    db.commit()
    return row


def delete_code(db: Session, code: str) -> bool:
    """사전에서 코드를 지운다. 기본 제공 코드는 지울 수 없다.

    막는 이유 — 지워도 그 코드로 오류는 계속 쌓입니다. 설명만 사라져
    「UG-CCTV-002 가 300건」이라는 뜻 모를 목록이 남습니다. 감추고 싶으면
    ``is_active`` 를 끄십시오.
    """
    row = get_code(db, code)
    if row is None:
        return False
    if row.builtin:
        raise ValueError("기본 제공 코드는 삭제할 수 없습니다. "
                         "목록에서 감추려면 「사용 안 함」으로 바꾸십시오.")
    db.delete(row)
    db.commit()
    return True


def code_usage(db: Session, *, days: int = 30) -> dict[str, int]:
    """코드별 최근 발생 건수. 사전 화면에서 「실제로 나는 오류」를 구분한다."""
    since = _now() - timedelta(days=days)
    rows = db.execute(
        select(ErrorLog.code, func.coalesce(func.sum(ErrorLog.count), 0))
        .where(ErrorLog.last_seen_at >= since)
        .group_by(ErrorLog.code)
    ).all()
    return {c: int(n) for c, n in rows}
