"""교통 관측 이력 — 강우×정체 판정을 쌓고 되짚는다 (2026-08-22 신설).

왜 필요한가
    ``traffic_observations`` 표는 2026-08-21 도메인 분리 때 만들어 뒀지만
    **기록하는 코드가 없어 한 건도 쌓이지 않았다**(모델 docstring 에
    "아직 기록하는 코드는 없습니다"라고 명시돼 있었고, 그 상태 그대로였다).
    그래서 교통 메뉴에 「분석 결과 이력」 화면을 만들 수도 없었다.

    이벤트는 위험할 때만 남는다 — 「봤는데 평온했다」가 빠지면 **평상시
    기준선을 만들 수 없다.** ``crowd_history`` 를 만든 것과 같은 이유다.

무엇을 위한 준비인가
    「이 지점은 비가 오면 늘 막힌다」 같은 판단은 과거가 있어야 가능하다.
    예측 모델보다 이력이 먼저다.

개인정보
    **사람·차량의 형상이나 식별 정보를 담지 않는다.** 대수·속도감소율 같은
    집계 수치만 남는다. 그래도 무한히 쌓지 않도록 보존기간을 둔다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import TrafficObservation

log = logging.getLogger("urbanguard.traffic_history")

# crowd 와 같은 기준 — 계절성(장마·행사철)을 보려면 최소 1년은 있어야 한다.
DEFAULT_KEEP_DAYS = 400
PURGE_BATCH = 2000
_DRIVERS_MAX = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(db: Session, *, camera_id: str, camera_name: str = "",
           snapshot: dict | None = None, source: str = "",
           failed: bool = False) -> TrafficObservation | None:
    """관측 한 건을 남긴다.

    :param snapshot: ``service/runner.py`` 의 ``_snapshot()`` 결과(또는 같은
        키를 가진 dict). 키 이름은 그 함수가 실제로 내는 것과 맞춰야 한다 —
        예전에 ``event_sync`` 에서 키를 잘못 읽어 detail 이 늘 비어 있던
        전례가 있다(2026-08-21).
    :param failed: 프레임을 못 받았으면 True — **정체 0 과 구분해야 한다.**
        「원활했다」와 「못 봤다」를 섞으면 기준선이 망가진다.

    실패는 삼키지 않는다 — 호출부(``event_sync``)가 이미 통째로 감싸고 있어
    여기서 또 삼키면 어디서 틀어졌는지 알 수 없다.
    """
    cam = (camera_id or "").strip()
    if not cam:
        return None
    s = snapshot or {}
    drivers = s.get("drivers") or []
    if isinstance(drivers, (list, tuple)):
        drivers = ", ".join(str(x) for x in drivers)

    row = TrafficObservation(
        camera_id=cam[:64], camera_name=(camera_name or cam)[:120],
        rain_mm_h=float(s.get("rain_mm_h") or 0.0),
        speed_drop=float(s.get("speed_drop") or 0.0),
        queue_len=int(s.get("queue_len") or 0),
        stalled_count=int(s.get("stalled") or 0),
        risk_code=str(s.get("risk_code") or "")[:32],
        # ⚠️ runner._snapshot() 은 이 값을 ``score`` 로 낸다(``risk_score``
        #   가 아니다). 키를 잘못 읽어 값이 늘 0 이던 전례가 있어 두 이름을
        #   모두 받아 둔다 — 실제 키는 ``score`` 다.
        risk_score=float(s.get("score") if s.get("score") is not None
                         else s.get("risk_score") or 0.0),
        severity=int(s.get("severity") or 0),
        drivers=str(drivers)[:_DRIVERS_MAX],
        failed=bool(failed),
        source=str(source or s.get("source_kind") or "")[:24],
        observed_at=_now())
    db.add(row)
    return row


def recent(db: Session, camera_id: str, *, limit: int = 200
           ) -> list[TrafficObservation]:
    """최근 관측부터. 화면과 예측이 같은 창을 본다.

    ⚠️ 시각 하나로 정렬하면 같은 초에 들어온 관측의 순서가 실행마다
    뒤집힌다 — ``id`` 를 2차 키로 두어 묶임을 확실히 깬다
    (``crowd_history.recent`` 가 같은 이유로 그렇게 한다).
    """
    return list(db.scalars(
        select(TrafficObservation)
        .where(TrafficObservation.camera_id == camera_id)
        .order_by(TrafficObservation.observed_at.desc(),
                  TrafficObservation.id.desc())
        .limit(max(1, limit))).all())


def series(db: Session, camera_id: str, *, hours: int = 24
           ) -> list[TrafficObservation]:
    """시간 오름차순. **예측 모델에 넣을 형태다.**

    ⚠️ **실패한 관측은 뺀다.** 못 본 구간을 0 으로 채우면 예측이 「그때는
    원활했다」로 학습한다.
    """
    since = _now() - timedelta(hours=max(1, hours))
    return list(db.scalars(
        select(TrafficObservation)
        .where(TrafficObservation.camera_id == camera_id,
               TrafficObservation.observed_at >= since,
               TrafficObservation.failed.is_(False))
        .order_by(TrafficObservation.observed_at.asc(),
                  TrafficObservation.id.asc())).all())


def baseline(db: Session, camera_id: str, *, hours: int = 24) -> dict:
    """평상시 기준선. **「평소보다 막히나」에 답하기 위한 것이다.**

    관측이 없으면 빈 dict 를 준다 — **0 을 돌려주면 「평소에 전혀 안
    막힌다」로 읽혀 지금 상태가 전부 급변으로 보인다.**
    """
    rows = series(db, camera_id, hours=hours)
    if not rows:
        return {}
    drops = sorted(r.speed_drop for r in rows)
    queues = sorted(r.queue_len for r in rows)
    mid = len(drops) // 2
    return {
        "samples": len(rows),
        "speed_drop_median": round(drops[mid], 3),
        "speed_drop_max": round(drops[-1], 3),
        "queue_median": queues[mid],
        "since": rows[0].observed_at.isoformat(),
    }


def purge(db: Session, *, keep_days: int = DEFAULT_KEEP_DAYS) -> int:
    """보존기간이 지난 관측을 지운다. 지운 건수를 돌려준다."""
    if keep_days <= 0:
        return 0
    cutoff = _now() - timedelta(days=keep_days)
    ids = list(db.scalars(
        select(TrafficObservation.id)
        .where(TrafficObservation.observed_at < cutoff)
        .limit(PURGE_BATCH)).all())
    if not ids:
        return 0
    db.execute(sa_delete(TrafficObservation)
               .where(TrafficObservation.id.in_(ids)))
    return len(ids)
