"""교대 인수인계 — 작성·제출·인수 확인 (S-04).

**왜 만들었나.** 관제는 24시간 이어지는데 사람은 8~12시간마다 바뀐다. 지금
그 이음매에는 아무것도 없어서, 「밤에 무슨 일이 있었는지」가 말과 수첩으로만
넘어간다. 야간에 열려 있던 이벤트가 주간 근무자에게 전달되지 않으면 그
이벤트는 아무도 안 보는 채로 남는다.

**미처리 이벤트는 자동으로 붙는다.** 사람이 손으로 옮겨 적으면 반드시 빠진다.
빠지는 것은 대체로 「별일 아니라고 생각한 것」이고, 사고는 거기서 난다.

**스냅샷으로 굳힌다.** 지금 열린 이벤트를 그때그때 조회하지 않고 작성 시점의
목록을 그대로 저장한다. 나중에 이벤트가 종결돼도 「인계 시점에 무엇이 열려
있었는가」가 남아야 하기 때문이다 — 사후 검토는 그 시점의 사실을 묻는다.

**지우지 않는다.** 인계 기록을 지울 수 있으면 「전달 못 받았다」는 다툼에
답할 근거가 사라진다(:class:`~.models.AuditLog` 와 같은 원칙).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import events as E
from . import sop as SOP
from .models import Event, ShiftHandover

# 상태
DRAFT = "draft"                # 작성중 — 아직 넘기지 않았다
SUBMITTED = "submitted"        # 인계함 — 받는 사람이 확인해야 한다
ACKNOWLEDGED = "acknowledged"  # 인수 확인 완료
STATUS_LABELS = {DRAFT: "작성중", SUBMITTED: "인계함", ACKNOWLEDGED: "인수 확인"}

# 근무조 이름 예시. 2교대·3교대·명칭이 기관마다 달라 **목록으로 고정하지
# 않는다** — 화면에서는 고를 수도 있고 직접 칠 수도 있게 둔다.
SHIFT_PRESETS = ("주간", "야간", "심야", "오전", "오후")

DEFAULT_LIMIT = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- 미처리 이벤트 스냅샷 -----------------------------------------------------

def snapshot_open_events(db: Session, *, allowed_domains: set[str] | None = None
                         ) -> dict:
    """지금 열려 있는 이벤트를 인계문에 붙일 형태로 굳힌다.

    SOP 이행 현황을 함께 담는다 — 인수자가 「무엇이 남았는지」를 이벤트마다
    눌러 보지 않고 한눈에 알아야 한다.
    """
    rows = E.list_events(db, status="active", allowed_domains=allowed_domains)
    items = []
    for ev in rows:
        p = SOP.progress(db, ev)
        items.append({
            "id": ev.id, "domain": ev.domain,
            "place": ev.place_name or ev.block_id,
            "level": ev.level, "peak_level": ev.peak_level,
            "status": ev.status,
            "detected_at": ev.detected_at.isoformat() if ev.detected_at else "",
            "elapsed": E.elapsed_text(ev),
            "sop_done": p["done"], "sop_total": p["total"],
            "sop_left": p["required_left"],
        })
    return {"taken_at": _now().isoformat(), "count": len(items),
            "items": items}


# --- 작성 -------------------------------------------------------------------

def create(db: Session, *, shift_date: datetime | None = None,
           shift_name: str = "", summary: str = "", todo: str = "",
           to_login: str = "", to_user_id: int | None = None,
           user=None, allowed_domains: set[str] | None = None
           ) -> ShiftHandover:
    """인계문을 만든다. 미처리 이벤트는 이 시점 기준으로 자동 첨부된다."""
    shift_name = (shift_name or "").strip()[:32]
    if not shift_name:
        raise ValueError("근무조를 입력하세요.")

    row = ShiftHandover(
        shift_date=shift_date or _now(), shift_name=shift_name,
        summary=(summary or "").strip()[:8000],
        todo=(todo or "").strip()[:8000],
        to_login=(to_login or "").strip()[:64], to_user_id=to_user_id,
        open_events=snapshot_open_events(db, allowed_domains=allowed_domains),
        status=DRAFT)
    if user is not None:
        row.from_user_id, row.from_login = user.id, user.login_id
    db.add(row)
    db.flush()
    return row


def update(db: Session, row_id: int, *, summary: str | None = None,
           todo: str | None = None, shift_name: str | None = None,
           to_login: str | None = None, to_user_id: int | None = None,
           refresh_events: bool = False,
           allowed_domains: set[str] | None = None) -> ShiftHandover:
    """작성중인 인계문을 고친다. **제출한 뒤에는 고칠 수 없다** — 넘긴 내용이
    나중에 바뀌면 인수자가 확인한 것이 무엇인지 알 수 없다."""
    row = db.get(ShiftHandover, row_id)
    if row is None:
        raise ValueError("대상 인계문을 찾을 수 없습니다.")
    if row.status != DRAFT:
        raise ValueError("이미 인계한 문서는 고칠 수 없습니다. "
                         "덧붙일 내용은 인수 확인 메모에 적으십시오.")
    if summary is not None:
        row.summary = summary.strip()[:8000]
    if todo is not None:
        row.todo = todo.strip()[:8000]
    if shift_name is not None and shift_name.strip():
        row.shift_name = shift_name.strip()[:32]
    if to_login is not None:
        row.to_login = to_login.strip()[:64]
        row.to_user_id = to_user_id
    if refresh_events:
        # 작성하는 동안 상황이 바뀔 수 있다. 제출 직전에 다시 뜨게 한다.
        row.open_events = snapshot_open_events(db,
                                               allowed_domains=allowed_domains)
    row.updated_at = _now()
    db.flush()
    return row


def submit(db: Session, row_id: int, *,
           allowed_domains: set[str] | None = None) -> ShiftHandover:
    """인계한다. 제출 시점으로 이벤트 스냅샷을 한 번 더 굳힌다 — 작성 시작
    시점이 아니라 **넘기는 시점**의 상황이 인계 대상이다."""
    row = db.get(ShiftHandover, row_id)
    if row is None:
        raise ValueError("대상 인계문을 찾을 수 없습니다.")
    if row.status != DRAFT:
        raise ValueError("이미 인계한 문서입니다.")
    if not (row.summary or "").strip():
        raise ValueError("인계 내용을 입력하세요. 빈 인계문은 넘길 수 없습니다.")
    row.open_events = snapshot_open_events(db, allowed_domains=allowed_domains)
    row.status = SUBMITTED
    row.submitted_at = _now()
    row.updated_at = row.submitted_at
    db.flush()
    return row


def acknowledge(db: Session, row_id: int, *, user=None,
                note: str = "") -> ShiftHandover:
    """인수 확인. **인계자 본인은 확인할 수 없다** — 혼자 넘기고 혼자 받으면
    인수인계가 아니다."""
    row = db.get(ShiftHandover, row_id)
    if row is None:
        raise ValueError("대상 인계문을 찾을 수 없습니다.")
    if row.status == DRAFT:
        raise ValueError("아직 인계되지 않은 문서입니다.")
    if row.status == ACKNOWLEDGED:
        raise ValueError("이미 인수 확인된 문서입니다.")
    if user is not None and row.from_user_id == user.id:
        raise ValueError("인계자 본인은 인수 확인할 수 없습니다.")

    row.status = ACKNOWLEDGED
    row.acknowledged_at = _now()
    row.ack_note = (note or "").strip()[:4000]
    if user is not None:
        # 확인한 사람이 곧 인수자다. 미리 적어 둔 이름이 있어도 실제로 받은
        # 사람으로 덮어쓴다 — 근무 교대는 계획대로 안 되는 일이 흔하다.
        row.to_user_id, row.to_login = user.id, user.login_id
    row.updated_at = row.acknowledged_at
    db.flush()
    return row


# --- 조회 -------------------------------------------------------------------

def get(db: Session, row_id: int) -> ShiftHandover | None:
    return db.get(ShiftHandover, row_id)


def search(db: Session, *, status: str = "", q: str = "", days: int = 0,
           limit: int = DEFAULT_LIMIT) -> list[ShiftHandover]:
    stmt = select(ShiftHandover)
    if status in STATUS_LABELS:
        stmt = stmt.where(ShiftHandover.status == status)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(ShiftHandover.shift_name.ilike(like),
                              ShiftHandover.from_login.ilike(like),
                              ShiftHandover.to_login.ilike(like),
                              ShiftHandover.summary.ilike(like),
                              ShiftHandover.todo.ilike(like)))
    if days > 0:
        stmt = stmt.where(ShiftHandover.shift_date >= _now() - timedelta(days=days))
    return list(db.scalars(stmt.order_by(ShiftHandover.shift_date.desc(),
                                         ShiftHandover.id.desc())
                           .limit(limit)).all())


def pending_ack(db: Session, *, exclude_user_id: int | None = None
                ) -> list[ShiftHandover]:
    """확인을 기다리는 인계문. 로그인하면 이것부터 보여야 한다."""
    stmt = select(ShiftHandover).where(ShiftHandover.status == SUBMITTED)
    if exclude_user_id is not None:
        # 내가 넘긴 것은 내가 확인할 수 없으므로 목록에서 뺀다.
        stmt = stmt.where(or_(ShiftHandover.from_user_id.is_(None),
                              ShiftHandover.from_user_id != exclude_user_id))
    return list(db.scalars(stmt.order_by(ShiftHandover.submitted_at)).all())


def my_draft(db: Session, user_id: int) -> ShiftHandover | None:
    """내가 쓰다 만 인계문. 한 사람이 여러 개를 붙들고 있으면 어느 것이
    진짜인지 알 수 없으므로 화면은 가장 최근 것 하나만 이어서 쓰게 한다."""
    return db.scalars(
        select(ShiftHandover)
        .where(ShiftHandover.status == DRAFT,
               ShiftHandover.from_user_id == user_id)
        .order_by(ShiftHandover.id.desc()).limit(1)).first()


def summary_counts(db: Session) -> dict:
    def n(status: str) -> int:
        return db.execute(select(func.count(ShiftHandover.id))
                          .where(ShiftHandover.status == status)).scalar() or 0
    return {"draft": n(DRAFT), "submitted": n(SUBMITTED),
            "acknowledged": n(ACKNOWLEDGED)}


def open_items(row: ShiftHandover) -> list[dict]:
    """스냅샷에서 이벤트 목록을 꺼낸다. 옛 기록은 형식이 다를 수 있으므로
    없으면 빈 목록으로 돌려준다 — 화면이 깨지는 것보다 낫다."""
    data = row.open_events or {}
    items = data.get("items")
    return items if isinstance(items, list) else []
