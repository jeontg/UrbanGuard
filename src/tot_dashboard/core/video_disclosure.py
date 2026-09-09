"""영상 반출 관리대장 — 등록·검색·정정 (S-94).

**왜 있는가.** 관제 영상을 밖으로 내주는 순간, 우리는 개인정보를 제3자에게
제공한 것이 된다. 개인정보 보호법과 「지방자치단체 영상정보처리기기 통합관제센터
구축 및 운영 규정」은 그 사실을 대장으로 남기라고 요구한다. 실무에서는 수사기관
공문·영장이 오면 담당 부서가 처리하고 대장을 손으로 적는 경우가 많은데, 그러면
연말 점검 때 「그 건은 누가 처리했더라」가 반복된다.

**설계 원칙 세 가지.**

1. **지우지 않는다.** 대장은 감사 자료다. 잘못 적었으면 :func:`correct` 로
   정정 사유를 남기고 새 줄을 만든다. 원본은 「정정됨」으로 표시만 된다.
   (:mod:`.errors` 와 반대다 — 오류 이력은 실제로 지운다.)
2. **근거 없이 등록할 수 없다.** ``legal_basis`` 가 비면 저장이 거부된다.
   근거를 못 적는 반출은 애초에 하면 안 되는 반출이다.
3. **파기까지 추적한다.** 내준 사본은 목적을 다하면 파기돼야 한다.
   ``disposal_due`` 가 지났는데 ``disposed_at`` 이 비어 있으면 화면에서
   「파기 지연」으로 뜬다 — 이것이 대장을 종이로 적을 때 가장 자주 빠지는 칸이다.

이 모듈은 **영상 파일을 다루지 않는다.** 실제 반출 처리는 사람이 하고, 여기는
그 사실의 기록만 맡는다. 파일까지 시스템이 내보내게 만들면 대장이 자동으로
채워져 편하지만, 「누가 승인했는가」가 사라진다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import Camera, VideoDisclosure

# 화면 한 번에 읽는 최대 행 수 (:mod:`.errors` 와 같은 값).
DEFAULT_LIMIT = 200

# 반출 형태. 원본 제공이 가장 위험하다 — 화면에서 순서대로 보여 준다.
METHODS = {
    "view": "열람 (제공 없음)",
    "copy": "사본 제공",
    "original": "원본 제공",
}

# 근거 유형. 자유 입력이지만 자주 쓰는 것을 골라 넣을 수 있게 둔다.
# 조문 번호는 기관마다 인용 방식이 달라 **일부러 넣지 않았다** — 틀린 조문이
# 기본값으로 박히면 대장 전체가 틀린 근거로 채워진다.
BASIS_PRESETS = [
    "수사기관 공문 (형사소송법)",
    "법원 영장",
    "정보주체 본인 열람 청구",
    "교통사고 처리 (보험·분쟁)",
    "재난 대응 기관 요청",
    "행정 목적 내부 활용",
]

# 파기 예정일 기본값. 목적을 다한 사본을 언제까지 두는가는 기관 규정이 정하는데,
# 규정이 없는 곳도 많아 **보수적으로 짧게** 잡았다. 화면에서 고칠 수 있다.
DEFAULT_DISPOSAL_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(s: str | None, limit: int) -> str:
    return (s or "").strip()[:limit]


# --- 등록 -------------------------------------------------------------------

def create(db: Session, *, requested_at: datetime | None = None,
           requester_org: str, requester_name: str = "",
           requester_contact: str = "", legal_basis: str, purpose: str = "",
           camera_ids: str = "", period_from: datetime | None = None,
           period_to: datetime | None = None, method: str = "view",
           masked: bool = True, handled_at: datetime | None = None,
           handler_id: int | None = None, handler_login: str = "",
           disposal_due: datetime | None = None, note: str = "",
           by: str = "", corrects_id: int | None = None,
           correction_reason: str = "") -> VideoDisclosure:
    """대장 한 줄을 남긴다. 검증에 걸리면 :class:`ValueError`.

    커밋은 **부르는 쪽이 한다** — 감사 로그와 한 트랜잭션으로 묶기 위해서다.
    """
    org = _clean(requester_org, 128)
    if not org:
        raise ValueError("요청 기관을 입력하세요.")
    basis = _clean(legal_basis, 255)
    if not basis:
        # 근거를 못 적는 반출은 하면 안 되는 반출이다. 여기서 막는다.
        raise ValueError("반출 근거를 입력하세요. 근거 없는 반출은 기록할 수 없습니다.")
    if method not in METHODS:
        raise ValueError(f"반출 형태가 올바르지 않습니다: {method}")
    if period_from and period_to and period_from > period_to:
        raise ValueError("촬영 기간의 시작이 종료보다 뒤입니다.")

    when = requested_at or _now()
    if disposal_due is None and method != "view":
        # 열람만 한 건은 남는 사본이 없으므로 파기 대상이 아니다.
        disposal_due = when + timedelta(days=DEFAULT_DISPOSAL_DAYS)

    row = VideoDisclosure(
        requested_at=when, requester_org=org,
        requester_name=_clean(requester_name, 64),
        requester_contact=_clean(requester_contact, 64),
        legal_basis=basis, purpose=_clean(purpose, 2000),
        camera_ids=_clean(camera_ids, 500),
        period_from=period_from, period_to=period_to,
        method=method, masked=bool(masked),
        handled_at=handled_at, handler_id=handler_id,
        handler_login=_clean(handler_login, 64),
        disposal_due=disposal_due, note=_clean(note, 2000),
        corrects_id=corrects_id,
        correction_reason=_clean(correction_reason, 2000),
        created_by=_clean(by, 64),
    )
    db.add(row)
    db.flush()
    return row


def correct(db: Session, origin_id: int, *, reason: str, by: str = "",
            **fields) -> VideoDisclosure:
    """기존 줄을 **고치지 않고** 정정본을 새로 만든다.

    원본은 그대로 두고 새 줄이 ``corrects_id`` 로 원본을 가리킨다. 화면은
    원본을 「정정됨」으로 흐리게 표시하고 정정본을 함께 보여 준다.
    """
    reason = _clean(reason, 2000)
    if not reason:
        raise ValueError("정정 사유를 입력하세요.")
    origin = db.get(VideoDisclosure, origin_id)
    if origin is None:
        raise ValueError("정정할 대상을 찾을 수 없습니다.")
    if corrected_by(db, origin_id) is not None:
        # 정정본이 또 정정되는 것은 막지 않지만, 원본이 두 번 정정되면
        # 어느 쪽이 유효한지 알 수 없다. 최신 정정본을 고치게 안내한다.
        raise ValueError("이미 정정된 기록입니다. 최신 정정본을 정정하세요.")

    base = {
        "requested_at": origin.requested_at,
        "requester_org": origin.requester_org,
        "requester_name": origin.requester_name,
        "requester_contact": origin.requester_contact,
        "legal_basis": origin.legal_basis, "purpose": origin.purpose,
        "camera_ids": origin.camera_ids,
        "period_from": origin.period_from, "period_to": origin.period_to,
        "method": origin.method, "masked": origin.masked,
        "handled_at": origin.handled_at, "handler_id": origin.handler_id,
        "handler_login": origin.handler_login,
        "disposal_due": origin.disposal_due, "note": origin.note,
    }
    # None 으로 넘어온 값은 「안 고침」이 아니라 「비움」일 수 있다. 폼에서
    # 넘어오지 않은 키는 아예 빠지므로, 들어온 키만 덮어쓴다.
    base.update(fields)
    return create(db, by=by, corrects_id=origin_id, correction_reason=reason,
                  **base)


def dispose(db: Session, row_id: int, *, when: datetime | None = None) -> bool:
    """파기 완료로 표시한다. 이것만은 원본 줄을 고친다 — 파기는 정정이 아니라
    **후속 사실의 추가**이고, 대장 한 줄이 곧 반출 한 건이어야 하기 때문이다."""
    row = db.get(VideoDisclosure, row_id)
    if row is None:
        return False
    row.disposed_at = when or _now()
    db.flush()
    return True


# --- 조회 -------------------------------------------------------------------

def get(db: Session, row_id: int) -> VideoDisclosure | None:
    return db.get(VideoDisclosure, row_id)


def corrected_by(db: Session, origin_id: int) -> VideoDisclosure | None:
    """이 줄을 정정한 줄. 없으면 ``None``."""
    return db.execute(
        select(VideoDisclosure)
        .where(VideoDisclosure.corrects_id == origin_id)
        .order_by(VideoDisclosure.id.desc()).limit(1)).scalars().first()


def search(db: Session, *, q: str = "", method: str = "",
           overdue: str = "", days: int = 0,
           limit: int = DEFAULT_LIMIT) -> list[VideoDisclosure]:
    """대장 검색. ``overdue="1"`` 이면 파기 기한이 지났는데 안 지운 건만."""
    stmt = select(VideoDisclosure)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(VideoDisclosure.requester_org.ilike(like),
                              VideoDisclosure.requester_name.ilike(like),
                              VideoDisclosure.legal_basis.ilike(like),
                              VideoDisclosure.purpose.ilike(like),
                              VideoDisclosure.camera_ids.ilike(like)))
    if method in METHODS:
        stmt = stmt.where(VideoDisclosure.method == method)
    if overdue == "1":
        stmt = stmt.where(VideoDisclosure.disposed_at.is_(None),
                          VideoDisclosure.disposal_due.is_not(None),
                          VideoDisclosure.disposal_due < _now())
    if days > 0:
        stmt = stmt.where(VideoDisclosure.requested_at >= _now() - timedelta(days=days))
    stmt = stmt.order_by(VideoDisclosure.requested_at.desc(),
                         VideoDisclosure.id.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def summary(db: Session) -> dict:
    """화면 위쪽 요약 숫자. 「파기 지연」이 0이 아니면 그것이 제일 급하다."""
    total = db.execute(select(func.count(VideoDisclosure.id))).scalar() or 0
    month = db.execute(
        select(func.count(VideoDisclosure.id))
        .where(VideoDisclosure.requested_at >= _now() - timedelta(days=30))
    ).scalar() or 0
    overdue = db.execute(
        select(func.count(VideoDisclosure.id))
        .where(VideoDisclosure.disposed_at.is_(None),
               VideoDisclosure.disposal_due.is_not(None),
               VideoDisclosure.disposal_due < _now())).scalar() or 0
    # 마스킹 없이 내준 건. 위법은 아니지만(본인 영상만 있는 경우 등) 왜 그랬는지
    # 설명할 수 있어야 하는 건이라 따로 센다.
    unmasked = db.execute(
        select(func.count(VideoDisclosure.id))
        .where(VideoDisclosure.masked.is_(False))).scalar() or 0
    return {"total": total, "month": month, "overdue": overdue,
            "unmasked": unmasked}


def camera_labels(db: Session) -> dict[str, str]:
    """``camera_ids`` 에 적힌 키를 사람이 읽는 이름으로 바꾸기 위한 표.

    카메라가 나중에 삭제돼도 대장은 남아야 하므로 **FK 를 걸지 않고** 키 문자열만
    저장한다. 이름을 못 찾으면 키를 그대로 보여 준다.
    """
    rows = db.execute(select(Camera.id, Camera.name)).all()
    return {k: (n or k) for k, n in rows}


def is_overdue(row: VideoDisclosure) -> bool:
    return (row.disposed_at is None and row.disposal_due is not None
            and row.disposal_due < _now())
