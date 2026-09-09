"""현장 제보 접수·검토 (S-43).

접수한 사진은 두 가지 목적을 동시에 갖는다.
  1. 보수 요청의 근거
  2. **부산 지역 노면 학습 데이터** — 해외 공개데이터 3회 실패의 우회 경로

두 번째가 성립하려면 담당자가 「학습에 쓸 수 있는 사진인가」를 판정해 줘야 한다.
초점이 나갔거나 노면이 안 보이는 사진은 학습을 오히려 망친다.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import image_mask
from .models import CitizenReport

log = logging.getLogger("urbanguard.reports")

RECEIVED = "received"
REVIEWED = "reviewed"
CONVERTED = "converted"
REJECTED = "rejected"
STATUS_LABELS = {RECEIVED: "접수", REVIEWED: "확인", CONVERTED: "보수 요청 전환",
                 REJECTED: "반려"}
OPEN_STATES = (RECEIVED, REVIEWED)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_BYTES = 12 * 1024 * 1024   # 휴대폰 사진 한 장 기준. 넘으면 거부한다.


def _now() -> datetime:
    return datetime.now(timezone.utc)


def photo_dir() -> Path:
    import os
    from ..common.config import PROJECT_ROOT
    override = os.environ.get("URBANGUARD_REPORT_DIR")
    base = Path(override) if override else PROJECT_ROOT / "data" / "reports_photo"
    base.mkdir(parents=True, exist_ok=True)
    return base


def validate_upload(filename: str, size: int) -> list[str]:
    errs = []
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        errs.append(f"지원하지 않는 형식입니다. {', '.join(sorted(ALLOWED_EXT))} 만 됩니다.")
    if size <= 0:
        errs.append("빈 파일입니다.")
    elif size > MAX_BYTES:
        errs.append(f"파일이 너무 큽니다({size // 1024 // 1024}MB). "
                    f"{MAX_BYTES // 1024 // 1024}MB 이하로 올려주세요.")
    return errs


def _safe_name(original: str) -> str:
    """저장 파일명. 원본 이름을 그대로 쓰지 않는다.

    한글·공백이 섞인 이름은 OpenCV 계열에서 조용히 실패하고, 사용자가 올린
    이름을 그대로 쓰면 경로 조작 위험도 생긴다.
    """
    ext = Path(original or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    return f"{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:12]}{ext}"


def create(db: Session, *, raw_path: Path, filename: str, user,
           place_name: str = "", block_id: str = "", description: str = "",
           lat: float | None = None, lng: float | None = None,
           domain: str = "road") -> CitizenReport:
    """제보를 접수한다. 사진은 **가려서만** 저장하고 원본은 지운다."""
    stored = photo_dir() / _safe_name(filename)
    status, count = image_mask.mask_people(raw_path, stored)
    try:
        raw_path.unlink(missing_ok=True)   # 원본은 남기지 않는다
    except Exception:  # noqa: BLE001
        log.exception("원본 임시 파일을 지우지 못했습니다: %s", raw_path)

    row = CitizenReport(
        domain=domain, place_name=place_name.strip(), block_id=block_id.strip(),
        lat=lat, lng=lng, description=description.strip(),
        photo_path=stored.name if stored.exists() else "",
        mask_status=status, mask_count=count, status=RECEIVED,
        reported_by=getattr(user, "id", None),
        login_id=getattr(user, "login_id", "") or "")
    db.add(row)
    db.flush()
    log.info("제보 접수 id=%s mask=%s(%d)", row.id, status, count)
    return row


def review(db: Session, report: CitizenReport, user, *, usable: bool,
           memo: str = "") -> None:
    report.status = REVIEWED
    report.usable_for_training = usable
    report.disposition = memo[:255]
    report.reviewed_by = getattr(user, "id", None)
    report.reviewed_at = _now()


def convert(db: Session, report: CitizenReport, user, event_id: int | None,
            memo: str = "") -> None:
    report.status = CONVERTED
    report.event_id = event_id
    report.disposition = memo[:255]
    report.reviewed_by = getattr(user, "id", None)
    report.reviewed_at = _now()


def reject(db: Session, report: CitizenReport, user, reason: str) -> None:
    report.status = REJECTED
    report.disposition = (reason or "사유 미기재")[:255]
    report.reviewed_by = getattr(user, "id", None)
    report.reviewed_at = _now()


def delete_photo(report: CitizenReport) -> bool:
    """사진 파기. 반려·개인정보 문제 시 즉시 지울 수 있어야 한다."""
    if not report.photo_path:
        return False
    p = photo_dir() / report.photo_path
    try:
        p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.exception("사진을 지우지 못했습니다: %s", p)
        return False
    report.photo_path = ""
    return True


def list_reports(db: Session, *, status: str = "", limit: int = 200
                 ) -> list[CitizenReport]:
    stmt = select(CitizenReport)
    if status == "open":
        stmt = stmt.where(CitizenReport.status.in_(OPEN_STATES))
    elif status:
        stmt = stmt.where(CitizenReport.status == status)
    return list(db.scalars(
        stmt.order_by(CitizenReport.created_at.desc(), CitizenReport.id.desc()).limit(limit)).all())


def counts(db: Session) -> dict[str, int]:
    out = {RECEIVED: 0, REVIEWED: 0, CONVERTED: 0, REJECTED: 0}
    for s in db.scalars(select(CitizenReport.status)).all():
        if s in out:
            out[s] += 1
    return out


def training_ready(db: Session) -> int:
    """학습에 쓸 수 있다고 판정된 제보 수 — 데이터 확보 진척의 지표."""
    rows = db.scalars(
        select(CitizenReport.id).where(CitizenReport.usable_for_training.is_(True),
                                       CitizenReport.photo_path != "")).all()
    return len(list(rows))


_SAFE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def safe_photo_path(name: str) -> Path | None:
    """사진 파일명 검증 — 경로 조작으로 다른 파일을 읽지 못하게 한다."""
    if not name or not _SAFE.match(name) or Path(name).suffix.lower() not in ALLOWED_EXT:
        return None
    p = (photo_dir() / name).resolve()
    if photo_dir().resolve() not in p.parents:
        return None
    return p if p.exists() else None
