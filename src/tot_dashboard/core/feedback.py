"""탐지 피드백 (S-07) — 관제요원의 판정을 학습으로 되돌리는 고리.

**왜 있는가.** 세 제안요청서가 정면으로 요구한다.

- 경남 SFR-003 「조치 결과(오탐, 실제 상황, 조치 완료)를 입력하여 **학습
  데이터로 활용**」
- 서울 SFR-009 「관제시스템 내 **미탐/오탐 보정** 기능 제공」
- 서울 SFR-015 「**관제 피드백 기반 AI 성능 고도화**」

우리는 노면 학습 프레임을 모으지만 **판정이 학습으로 되돌아가는 경로가
없었다.** 이 모듈이 그 자리다.

⚠️ **판정했다고 자동으로 학습되지 않는다.** ``used_for_training`` 이 따로 있고,
그 사이에 사람이 라벨을 확인하는 단계가 있다. 관제요원의 한 번 클릭이 곧바로
모델을 바꾸면, 잘못 찍은 한 건이 모델을 망친다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (VERDICT_FALSE, VERDICT_MISSED, VERDICT_TRUE,
                     VERDICT_UNCLEAR, DetectionFeedback, Event)

VERDICTS = (VERDICT_TRUE, VERDICT_FALSE, VERDICT_UNCLEAR, VERDICT_MISSED)

VERDICT_LABELS = {
    VERDICT_TRUE: "실제 상황",
    VERDICT_FALSE: "오탐",
    VERDICT_UNCLEAR: "판단 보류",
    VERDICT_MISSED: "미탐 신고",
}

# 이벤트에 붙일 수 있는 판정. 미탐은 이벤트가 없으므로 뺀다.
EVENT_VERDICTS = (VERDICT_TRUE, VERDICT_FALSE, VERDICT_UNCLEAR)

# 오탐이라고 할 때 이유를 받는다. 「무엇을 잘못 봤는가」가 모델 개선의 알맹이라,
# 이유 없는 오탐 판정은 숫자만 남고 쓸 데가 없다.
REASON_REQUIRED = (VERDICT_FALSE, VERDICT_MISSED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(db: Session, *, verdict: str, user=None, event: Event | None = None,
           camera_id: str = "", domain: str = "", hazard_type_code: str = "",
           level: str = "", reason: str = "",
           occurred_at: datetime | None = None) -> tuple[DetectionFeedback | None,
                                                          list[str]]:
    """판정을 남긴다. ``(행, 오류목록)``.

    ⚠️ **고쳐 쓰지 않고 새로 쌓는다.** 「처음엔 오탐이라 했다가 정탐으로
    바꿨다」는 사실 자체가 모델 평가에 필요한 정보다.
    """
    errs: list[str] = []
    if verdict not in VERDICTS:
        return None, ["판정값이 올바르지 않습니다."]
    if verdict in REASON_REQUIRED and not (reason or "").strip():
        errs.append(f"「{VERDICT_LABELS[verdict]}」은 사유를 적어야 합니다 — "
                    "무엇을 잘못 봤는지가 모델 개선의 알맹이입니다.")
    if verdict == VERDICT_MISSED:
        if not (camera_id or "").strip():
            errs.append("미탐 신고는 지점을 골라야 합니다.")
        if occurred_at is None:
            errs.append("미탐 신고는 발생 시각을 적어야 합니다.")
    elif event is None:
        errs.append("판정할 이벤트를 찾을 수 없습니다.")
    if errs:
        return None, errs

    row = DetectionFeedback(
        event_id=event.id if event is not None else None,
        camera_id=(camera_id or (event.block_id if event else "")).strip(),
        domain=(domain or (event.domain if event else "")).strip(),
        hazard_type_code=(hazard_type_code
                          or (getattr(event, "hazard_type_code", "") or "")
                          if event is not None else hazard_type_code).strip(),
        # 판정 당시 등급을 굳혀 둔다 — 이벤트 등급은 나중에 바뀐다.
        level=(level or (event.level if event else "")).strip(),
        verdict=verdict,
        reason=(reason or "").strip(),
        occurred_at=occurred_at or (event.detected_at if event else _now()),
        user_id=getattr(user, "id", None),
        login_id=getattr(user, "login_id", "") or "",
    )
    db.add(row)

    # 이벤트의 오탐 표시는 **최신 판정을 따라간다.** 화면이 이 값을 쓰고 있어
    # 맞춰 두지 않으면 목록과 판정이 어긋난다.
    if event is not None and verdict in (VERDICT_TRUE, VERDICT_FALSE):
        event.false_positive = (verdict == VERDICT_FALSE)

    db.flush()
    return row, []


def latest_for_event(db: Session, event_id: int) -> DetectionFeedback | None:
    """이벤트의 **가장 최근** 판정. 이력은 남기되 화면은 최신만 보여 준다."""
    return db.scalars(
        select(DetectionFeedback)
        .where(DetectionFeedback.event_id == event_id)
        .order_by(DetectionFeedback.created_at.desc(),
                  DetectionFeedback.id.desc())
        .limit(1)).first()


def history_for_event(db: Session, event_id: int) -> list[DetectionFeedback]:
    """판정 이력 전부(최신 먼저). 바뀐 판정을 추적할 때 쓴다."""
    return list(db.scalars(
        select(DetectionFeedback)
        .where(DetectionFeedback.event_id == event_id)
        .order_by(DetectionFeedback.created_at.desc(),
                  DetectionFeedback.id.desc())))


def recent(db: Session, *, limit: int = 50, verdict: str = "",
           domain: str = "") -> list[DetectionFeedback]:
    q = select(DetectionFeedback)
    if verdict:
        q = q.where(DetectionFeedback.verdict == verdict)
    if domain:
        q = q.where(DetectionFeedback.domain == domain)
    return list(db.scalars(
        q.order_by(DetectionFeedback.created_at.desc(),
                   DetectionFeedback.id.desc()).limit(limit)))


def counts(db: Session, *, domain: str = "") -> dict:
    """판정별 건수와 오탐률.

    ⚠️ **오탐률 계산에서 「판단 보류」와 「미탐」을 뺀다.** 판단 보류는 정탐도
    오탐도 아니고, 미탐은 애초에 이벤트가 없어 분모가 다르다. 섞으면 숫자가
    무슨 뜻인지 아무도 모르게 된다.
    """
    q = select(DetectionFeedback.verdict, func.count(DetectionFeedback.id))
    if domain:
        q = q.where(DetectionFeedback.domain == domain)
    rows = dict(db.execute(q.group_by(DetectionFeedback.verdict)).all())
    got = {v: int(rows.get(v, 0)) for v in VERDICTS}
    judged = got[VERDICT_TRUE] + got[VERDICT_FALSE]
    got["judged"] = judged
    got["total"] = sum(int(n) for n in rows.values())
    # 판정이 없으면 비율을 만들지 않는다. 0% 는 「오탐이 없다」로 읽히는데,
    # 실제로는 「아직 아무도 안 봤다」다.
    got["false_rate"] = round(got[VERDICT_FALSE] / judged * 100, 1) if judged else None
    return got


def counts_by_hazard(db: Session, *, domain: str = "",
                     since: datetime | None = None) -> list[dict]:
    """(도메인, 위험유형) 별 판정 건수와 오탐률 — Phase 3 (2026-08-26).

    ``counts()`` 와 같은 규칙을 유형 단위로 쪼갠다. 유형별로 쪼개는 이유는
    ``docs/202608260842/`` 조사에서 나왔다 — 「교통위험 전체 오탐률」한
    줄로는 강우정체와 보행자 오탐이 섞여, 어느 판정 로직을 손봐야 하는지
    알 수 없다.

    ⚠️ **이 표도 관제요원이 판정한 건만 센다.** 판정이 없는 유형은 여기
    아예 안 나온다 — 0건을 「오탐 없음」으로 읽으면 안 되므로, 호출부가
    표본 수(``judged``)를 보고 신뢰 가능 여부를 스스로 판단해야 한다.

    ``since`` 는 이벤트/사건이 **발생한 시각**(``occurred_at``) 기준이다 —
    판정을 남긴 시각(``created_at``)으로 자르면, 기간이 지난 뒤 뒤늦게
    판정한 오래된 사건이 이번 기간 통계에 섞여 들어간다.
    """
    q = select(DetectionFeedback.domain, DetectionFeedback.hazard_type_code,
              DetectionFeedback.verdict, func.count(DetectionFeedback.id))
    if domain:
        q = q.where(DetectionFeedback.domain == domain)
    if since is not None:
        q = q.where(DetectionFeedback.occurred_at >= since)
    q = q.group_by(DetectionFeedback.domain, DetectionFeedback.hazard_type_code,
                   DetectionFeedback.verdict)

    agg: dict[tuple[str, str], dict[str, int]] = {}
    for dom, code, verdict, n in db.execute(q).all():
        key = (dom, code or "")
        bucket = agg.setdefault(key, {v: 0 for v in VERDICTS})
        bucket[verdict] = bucket.get(verdict, 0) + int(n)

    out = []
    for (dom, code), got in agg.items():
        judged = got[VERDICT_TRUE] + got[VERDICT_FALSE]
        out.append({
            "domain": dom, "hazard_type_code": code,
            "true": got[VERDICT_TRUE], "false": got[VERDICT_FALSE],
            "unclear": got[VERDICT_UNCLEAR], "missed": got[VERDICT_MISSED],
            "judged": judged, "total": sum(got.values()),
            "false_rate": round(got[VERDICT_FALSE] / judged * 100, 1) if judged else None,
        })
    return out


def pending_events(db: Session, *, limit: int = 50,
                   allowed_domains: set[str] | None = None) -> list[Event]:
    """아직 판정이 없는 종결 이벤트.

    **종결된 것만** 고른다 — 진행 중인 사건을 두고 오탐이냐 묻는 것은 순서가
    틀렸다. 관제요원은 지금 대응해야 한다.
    """
    judged = select(DetectionFeedback.event_id).where(
        DetectionFeedback.event_id.is_not(None))
    q = (select(Event)
         .where(Event.status == "closed", Event.id.not_in(judged))
         .order_by(Event.closed_at.desc().nulls_last(), Event.id.desc())
         .limit(limit))
    if allowed_domains is not None:
        q = q.where(Event.domain.in_(list(allowed_domains) or [""]))
    return list(db.scalars(q))
