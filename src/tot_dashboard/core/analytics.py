"""S-60 통계·성과 리포트 · S-61 AI 모델 운영.

**S-60** — 지자체는 연말 성과 보고가 필요하고, 그 숫자가 다음 해 예산 근거가
된다. 우리가 이 화면을 주면 담당자의 실무 부담을 직접 줄이는 것이라 재계약에
유리하다(설계서 5절).

**S-61** — 우리 모델은 부산 실환경 검증 전이다. 오탐률을 계속 보지 않으면
성능 저하를 눈치채지 못한다. 오탐 신고(S-03)가 여기 집계되어 재학습 근거가 된다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import events as E
from . import roles as R
from .models import CitizenReport, Event, EventAction, HazardType, Notification

PERIODS = {"7d": ("최근 7일", 7), "30d": ("최근 30일", 30),
           "90d": ("최근 90일", 90), "365d": ("최근 1년", 365)}
DEFAULT_PERIOD = "30d"


def period_start(period: str) -> datetime:
    days = PERIODS.get(period, PERIODS[DEFAULT_PERIOD])[1]
    return datetime.now(timezone.utc) - timedelta(days=days)


def _aware(dt: datetime | None) -> datetime | None:
    """시간대를 UTC 로 통일한다.

    PostgreSQL 은 timestamptz 를 **서버 시간대**(여기서는 Asia/Seoul)로 돌려준다.
    UTC 기준으로 만든 날짜 키와 섞이면 자정 근처 건이 하루 어긋나 통계가 밀린다.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def summary(db: Session, period: str = DEFAULT_PERIOD,
            allowed_domains: set[str] | None = None) -> dict:
    """기간별 탐지·대응 요약.

    대응 소요시간은 **탐지 → 첫 확인** 까지로 잰다. 종결까지로 재면 야간에
    넘어간 건이 통계를 왜곡한다(교대 인수인계로 다음 조가 이어받기 때문).
    """
    since = period_start(period)
    stmt = select(Event).where(Event.detected_at >= since)
    if allowed_domains is not None:
        stmt = stmt.where(Event.domain.in_(allowed_domains or {"__none__"}))
    events = list(db.scalars(stmt).all())

    by_domain: dict[str, int] = {}
    by_level: dict[str, int] = {}
    closed = 0
    false_pos = 0
    for ev in events:
        by_domain[ev.domain] = by_domain.get(ev.domain, 0) + 1
        lv = ev.peak_level or ev.level or "미상"
        by_level[lv] = by_level.get(lv, 0) + 1
        if ev.status == E.CLOSED:
            closed += 1
        if ev.false_positive:
            false_pos += 1

    # 첫 확인까지의 소요시간
    ack_minutes: list[float] = []
    if events:
        ids = [e.id for e in events]
        acks = db.scalars(
            select(EventAction).where(EventAction.event_id.in_(ids),
                                      EventAction.action == E.ACT_ACKNOWLEDGE)
            .order_by(EventAction.created_at)).all()
        first: dict[int, datetime] = {}
        for a in acks:
            first.setdefault(a.event_id, _aware(a.created_at))
        for ev in events:
            t = first.get(ev.id)
            if t:
                ack_minutes.append((t - _aware(ev.detected_at)).total_seconds() / 60)

    notif = db.scalars(
        select(Notification).where(Notification.requested_at >= since)).all()
    notif_sent = sum(1 for n in notif if n.status == "sent")

    return {
        "period": period,
        "period_label": PERIODS.get(period, PERIODS[DEFAULT_PERIOD])[0],
        "since": since,
        "total": len(events),
        "closed": closed,
        "open": len(events) - closed,
        "close_rate": round(closed / len(events) * 100, 1) if events else 0.0,
        "false_positive": false_pos,
        "false_positive_rate": (round(false_pos / len(events) * 100, 1)
                                if events else 0.0),
        "by_domain": [
            {"domain": d,
             "label": R.DOMAIN_LABELS.get(R.Domain(d), d)
             if d in {x.value for x in R.Domain} else d,
             "count": c}
            for d, c in sorted(by_domain.items(), key=lambda kv: -kv[1])],
        "by_level": [{"level": lv, "count": c}
                     for lv, c in sorted(by_level.items(),
                                         key=lambda kv: -E.rank(kv[0]))],
        "ack_count": len(ack_minutes),
        "ack_avg_min": round(sum(ack_minutes) / len(ack_minutes), 1) if ack_minutes else None,
        "ack_max_min": round(max(ack_minutes), 1) if ack_minutes else None,
        "notify_requested": len(notif),
        "notify_sent": notif_sent,
    }


def daily_counts(db: Session, period: str = DEFAULT_PERIOD,
                 allowed_domains: set[str] | None = None) -> list[dict]:
    """일자별 탐지 건수 — 추이를 보려면 막대 하나로는 부족하다."""
    since = period_start(period)
    stmt = select(Event.detected_at).where(Event.detected_at >= since)
    if allowed_domains is not None:
        stmt = stmt.where(Event.domain.in_(allowed_domains or {"__none__"}))
    buckets: dict[str, int] = {}
    for dt in db.scalars(stmt).all():
        key = _aware(dt).strftime("%m-%d")
        buckets[key] = buckets.get(key, 0) + 1
    days = PERIODS.get(period, PERIODS[DEFAULT_PERIOD])[1]
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%m-%d")
        out.append({"date": d, "count": buckets.get(d, 0)})
    # 1년치는 막대가 너무 많아 읽히지 않는다 — 최근 60일만 그린다.
    return out[-60:]


# --- 유형별 오탐률 (Phase 3, 2026-08-26) ------------------------------------
#
# ★ 왜 새 백엔드가 필요 없는가
#     ``feedback.record()``가 ``event.hazard_type_code``를 판정 시점에 이미
#     굳혀 왔다 — 입력은 Phase 0(위험유형 배관)이 끝나는 순간부터 저절로
#     쌓인다. 여기서 하는 일은 그 입력을 **유형별로 묶어 노출하는 것**뿐이다.
#
# ★ 정직한 1차 산출물은 「유형별 오탐률을 보이게 만드는 것」이지 「정확도
#     96%」가 아니다 — 재현율(미탐률)의 분모(실제 발생한 전체 사건)를 모르고,
#     우리가 아는 미탐은 「사람이 알아챈 미탐」뿐이라 늘 과소 집계된다.
#     그래서 여기는 **오탐률만** 낸다.
MIN_SAMPLE = 20  # [자체] 이 미만이면 비율 대신 표본 수만 믿을 수 있다


def _domain_label(domain: str) -> str:
    return (R.DOMAIN_LABELS.get(R.Domain(domain), domain)
            if domain in {x.value for x in R.Domain} else domain)


def detection_quality(db: Session, period: str = DEFAULT_PERIOD,
                      allowed_domains: set[str] | None = None) -> list[dict]:
    """유형별 오탐률 — 판정 많은 순.

    표본이 :data:`MIN_SAMPLE` 미만이면 ``low_sample=True``를 함께 준다.
    화면은 이 값이 참이면 비율 대신 표본 수를 보여야 한다 — 5건 중 0건
    오탐을 "오탐률 0%"라고 하면 실제보다 좋아 보인다.
    """
    from . import feedback as FB

    since = period_start(period)
    rows = FB.counts_by_hazard(db, since=since)
    if allowed_domains is not None:
        rows = [r for r in rows if r["domain"] in allowed_domains]

    labels = {h.code: h.label for h in db.scalars(select(HazardType)).all()}
    out = []
    for r in rows:
        code = r["hazard_type_code"]
        out.append({
            **r,
            "label": labels.get(code, code or "미분류"),
            "domain_label": _domain_label(r["domain"]),
            "low_sample": r["judged"] < MIN_SAMPLE,
        })
    out.sort(key=lambda r: -r["total"])
    return out


# --- S-61 모델 운영 ----------------------------------------------------------
#
# ⚠️ 예전에는 여기에 버전·상태·비고가 **문자열로 박혀** 있었다. 그래서 모델을
# 더 나은 것으로 바꿔도 화면은 계속 「v0.3 · 개발중」이었다. S-01 노면 카드에서
# 걷어냈던 것과 같은 부류의 결함이라 함께 걷어냈다.
#
# 지금은 세 가지를 구분한다.
#   * **사실**   — 모델 파일이 있는가, 얼마나 크고 언제 학습됐나 (레지스트리)
#   * **실적**   — 이 도메인이 실제로 몇 건 탐지했고 몇 건이 오탐 신고됐나 (DB)
#   * **판단**   — 「부산 CCTV 실사용 불가」 같은 사람의 결론 (설정값, 화면에서 편집)
# 앞의 둘은 계산하고, 마지막은 사람이 적는다. 계산할 수 없는 것을 계산한 척하지 않는다.

MODEL_DOMAINS = [
    {"key": "flood", "name": "침수 물 세그멘테이션", "domain": "flood"},
    # ⚠️ traffic 이 빠져 있었다(2026-08-22 전수점검) — 2026-08-21 도메인
    #   분리 이후 신설된 도메인인데 이 목록만 갱신을 안 해서, /models 화면의
    #   현황표에서 교통 이벤트의 탐지·오탐 집계가 통째로 빠지고 있었다.
    {"key": "traffic", "name": "교통위험 차량 검출", "domain": "traffic"},
    {"key": "crowd", "name": "인파 검출", "domain": "crowd"},
    {"key": "road", "name": "노면 손상 탐지", "domain": "road"},
]

# 상태 — 파일과 실적에서 도출한다.
ST_MISSING = "모델 없음"        # 지정된 파일이 없다. 탐지가 아예 안 된다
ST_UNTESTED = "탐지 실적 없음"  # 돌긴 하는데 아직 한 건도 못 잡았다
ST_RUNNING = "운영중"


def _model_state(info, detected: int) -> tuple[str, str]:
    """(상태, 배지 종류). 「운영중」을 함부로 붙이지 않는다.

    탐지 0건을 「운영중」으로 표시하면, 모델이 아무것도 못 잡고 있는 상태와
    평온해서 잡을 것이 없는 상태가 같아 보인다. 노면이 정확히 그 경우다.
    """
    if info is None or not info.exists:
        return ST_MISSING, "crit"
    if detected <= 0:
        return ST_UNTESTED, "warn"
    return ST_RUNNING, "on"


def model_stats(db: Session) -> list[dict]:
    """모델별 탐지·오탐 집계와 현재 운영 모델.

    오탐률은 **관제요원이 신고한 건수 기준**이다. 자동으로 알 수 있는 값이
    아니므로, 신고가 적으면 오탐률이 낮은 게 아니라 **아직 모르는 것**이다.
    """
    from . import model_ops

    rows = []
    for m in MODEL_DOMAINS:
        dom = m["domain"]
        total = len(list(db.scalars(
            select(Event.id).where(Event.domain == dom)).all()))
        fp = len(list(db.scalars(
            select(Event.id).where(Event.domain == dom,
                                   Event.false_positive.is_(True))).all()))
        try:
            info = model_ops.selected(dom, db)
        except Exception:  # noqa: BLE001
            info = None
        state, badge = _model_state(info, total)
        rows.append({
            **m,
            # 「버전」은 이제 실제 파일이다. 손으로 적은 숫자가 아니다.
            "version": (f"{info.label} · {info.size_mb}MB" if info
                        else "지정된 모델 없음"),
            "model_key": info.key if info else "",
            "model_exists": bool(info and info.exists),
            "modified": (info.modified.strftime("%Y-%m-%d")
                         if info and info.modified else ""),
            "state": state, "state_badge": badge,
            "note": model_ops.note(dom, db),
            "detected": total,
            "false_positive": fp,
            "fp_rate": round(fp / total * 100, 1) if total else None,
        })
    return rows


def training_data_status(db: Session) -> dict:
    """재학습 데이터 확보 현황 — 도로 도메인의 유일한 병목."""
    usable = len(list(db.scalars(
        select(CitizenReport.id).where(CitizenReport.usable_for_training.is_(True),
                                       CitizenReport.photo_path != "")).all()))
    total = len(list(db.scalars(select(CitizenReport.id)).all()))
    return {"usable": usable, "total": total}


def false_positive_events(db: Session, limit: int = 50) -> list[Event]:
    return list(db.scalars(
        select(Event).where(Event.false_positive.is_(True))
        .order_by(Event.closed_at.desc()).limit(limit)).all())
