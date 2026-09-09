"""인파 관측 이력 — 흐름 지표를 쌓고 되짚는다.

왜 필요한가
    흐름 지표(속도·방향분산·발산도)를 **매 프레임 계산해 놓고 버리고**
    있었습니다. 화면에는 「지금」만 보이고, 위험행동이 잡혔을 때만 이벤트가
    남습니다. 그래서 **「평소보다 붐비나」에 답할 수 없었습니다.**

    ``surge`` 는 이름 그대로 「평소 대비 배수」인데 그 「평소」가 **프로세스
    메모리의 최근 60건**뿐이라 재시작하면 사라집니다.

무엇을 위한 준비인가
    시계열 예측(「15분 내 90% 용량」식)은 과거가 있어야 가능합니다.
    **예측 모델보다 이력이 먼저입니다** — 오늘 쌓기 시작해야 다음 달에
    씁니다.

개인정보
    **사람의 형상이나 식별 정보를 담지 않습니다.** 인원수와 흐름 수치만
    남으므로 영상·이미지와 성격이 다릅니다. 그래도 무한히 쌓지 않도록
    보존기간을 둡니다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CrowdObservation

log = logging.getLogger("urbanguard.crowd_history")

# 기본 보존기간. 계절성(주중·주말, 행사철)을 보려면 최소 1년은 있어야 한다.
DEFAULT_KEEP_DAYS = 400

# 한 번에 지우는 최대 건수. 오래 방치된 서버에서 수십만 건이 한꺼번에 걸리면
# 트랜잭션이 길어져 다른 작업을 막는다.
PURGE_BATCH = 2000

# 근거 문자열 길이 상한(컬럼과 맞춘다).
_DRIVERS_MAX = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(db: Session, *, camera_id: str, camera_name: str = "",
           snapshot: dict | None = None, source: str = "",
           failed: bool = False) -> CrowdObservation | None:
    """관측 한 건을 남긴다.

    :param snapshot: :meth:`CrowdSnapshot.to_dict` 결과.
    :param failed: 프레임을 못 받았으면 True — **인원 0 과 구분해야 한다.**
        「사람이 없었다」와 「못 봤다」를 섞으면 기준선이 망가진다.

    실패는 삼킨다 — **이력 기록이 관제를 멈추면 안 된다.**
    """
    cam = (camera_id or "").strip()
    if not cam:
        return None
    s = snapshot or {}
    drivers = s.get("drivers") or []
    if isinstance(drivers, (list, tuple)):
        drivers = ", ".join(str(x) for x in drivers)

    row = CrowdObservation(
        camera_id=cam[:64], camera_name=(camera_name or cam)[:120],
        person_count=int(s.get("person_count") or 0),
        density_index=float(s.get("density_index") or 0.0),
        mean_speed=float(s.get("mean_speed") or 0.0),
        # 1.0 이 평상시다. 없으면 0 이 아니라 1.0 으로 둔다.
        surge=float(s.get("surge") if s.get("surge") is not None else 1.0),
        dispersion=float(s.get("trajectory_variance")
                         if s.get("trajectory_variance") is not None
                         else s.get("dispersion") or 0.0),
        divergence=float(s.get("divergence") or 0.0),
        risk_code=str(s.get("risk_code") or "")[:32],
        risk_score=float(s.get("risk_score") or 0.0),
        severity=int(s.get("severity") or 0),
        drivers=str(drivers)[:_DRIVERS_MAX],
        failed=bool(failed),
        source=str(source or s.get("source") or "")[:24],
        observed_at=_now())
    db.add(row)
    return row


def age_seconds(observed_at) -> float | None:
    """관측 시각으로부터 지난 시간(초). ``road/results.py::age_seconds``와
    같은 이유로 ``None``을 0으로 바꾸지 않는다 — 0초는 「방금 관측함」이라는
    뜻이라, 관측이 아예 없는 상태와 섞이면 오래된 카메라가 최신으로 보인다.
    """
    if observed_at is None:
        return None
    t = observed_at
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max((_now() - t).total_seconds(), 0.0)


def summary(db: Session, camera_id: str, name: str) -> dict:
    """실시간 관제 카드 한 장에 필요한 값 (2026-08-27, 인파 상시 카메라별
    모니터링 그리드 신설). ``road/results.py::summary``와 같은 모양이다 —
    관측이 없으면 등급을 매기지 않는다(관측 안 함과 「정상」을 섞지 않는다).
    """
    rows = recent(db, camera_id, limit=1)
    if not rows:
        return {"camera_id": camera_id, "name": name, "observed": False,
                "failed": False, "person_count": None, "density_index": None,
                "mean_speed": None, "risk_code": None, "risk_score": None,
                "severity": None, "drivers": "", "source": None,
                "observed_at": None, "age_sec": None}
    r = rows[0]
    return {
        "camera_id": camera_id, "name": name, "observed": True,
        "failed": bool(r.failed),
        "person_count": r.person_count, "density_index": r.density_index,
        "mean_speed": r.mean_speed, "surge": r.surge,
        "divergence": r.divergence,
        "risk_code": r.risk_code, "risk_score": r.risk_score,
        "severity": r.severity, "drivers": r.drivers or "",
        "source": r.source or "",
        "observed_at": r.observed_at.isoformat() if r.observed_at else None,
        "age_sec": age_seconds(r.observed_at),
    }


def recent(db: Session, camera_id: str, *, limit: int = 200
           ) -> list[CrowdObservation]:
    """최근 관측부터. 화면과 예측이 같은 창을 본다."""
    return list(db.scalars(
        select(CrowdObservation)
        .where(CrowdObservation.camera_id == camera_id)
        # ⚠️ **시각 하나로 정렬하면 순서가 보장되지 않는다.**
        #    같은 초에 들어온 관측은 서로 순위가 없어(tie), 실행마다 순서가
        #    뒤집힌다. 실제로 「최근이 먼저」를 검사하는 시험이 **4, 6, 8 중
        #    4를 먼저** 받아 깨졌다(2026-08-20 전체 점검).
        #
        # ★ 운영에서도 같은 일이 난다 — 관제요원이 「가장 최근 관측」이라고
        #   믿는 값이 사실은 그 초의 아무 것이 된다. `id` 는 단조 증가하므로
        #   묶인 순서를 확실히 깬다.
        .order_by(CrowdObservation.observed_at.desc(),
                  CrowdObservation.id.desc())
        .limit(max(1, limit))).all())


def series(db: Session, camera_id: str, *, hours: int = 24
           ) -> list[CrowdObservation]:
    """시간 오름차순. **예측 모델에 넣을 형태다.**

    ⚠️ **실패한 관측은 뺀다.** 못 본 구간을 0 으로 채우면 예측이 「사람이
    빠졌다」로 학습한다.
    """
    since = _now() - timedelta(hours=max(1, hours))
    return list(db.scalars(
        select(CrowdObservation)
        .where(CrowdObservation.camera_id == camera_id,
               CrowdObservation.observed_at >= since,
               CrowdObservation.failed.is_(False))
        # 같은 이유로 2차 키를 둔다(위 주석 참고). 시계열이라 오름차순이다.
        .order_by(CrowdObservation.observed_at.asc(),
                  CrowdObservation.id.asc())).all())


def baseline(db: Session, camera_id: str, *, hours: int = 24) -> dict:
    """평상시 기준선. **「평소보다 붐비나」에 답하기 위한 것이다.**

    관측이 없으면 빈 dict 를 준다 — **0 을 돌려주면 「평소에 아무도 없다」로
    읽혀 지금 인원이 전부 급증으로 보인다.**
    """
    rows = series(db, camera_id, hours=hours)
    if not rows:
        return {}
    counts = sorted(r.person_count for r in rows)
    speeds = sorted(r.mean_speed for r in rows)
    mid = len(counts) // 2
    return {
        "samples": len(rows),
        "person_median": counts[mid],
        "person_max": counts[-1],
        "speed_median": round(speeds[mid], 2),
        "since": rows[0].observed_at.isoformat(),
    }


def purge(db: Session, *, keep_days: int = DEFAULT_KEEP_DAYS) -> int:
    """보존기간이 지난 관측을 지운다. 지운 건수를 돌려준다."""
    if keep_days <= 0:
        return 0
    cutoff = _now() - timedelta(days=keep_days)
    ids = list(db.scalars(
        select(CrowdObservation.id)
        .where(CrowdObservation.observed_at < cutoff)
        .limit(PURGE_BATCH)).all())
    if not ids:
        return 0
    db.execute(sa_delete(CrowdObservation)
               .where(CrowdObservation.id.in_(ids)))
    return len(ids)


def severity_spread(db: Session, camera_id: str = "", *, days: int = 30) -> dict | None:
    """등급이 실제로 **갈리고 있는가**. 못 읽으면 ``None``.

    ★ **왜 필요한가 (2026-08-19 전수조사)**

    실측해 보니 인파 관측 **670회 중 627회(93.6%)가 같은 등급**이었다.
    거의 언제나 「이동흐름혼란」이 떴다.

    원인을 찾아보니 판정 문턱 ``disp_hi=0.6`` 이 방아쇠인데, 실측
    ``dispersion`` 평균이 **0.888**(94%가 문턱 초과)이었다. 다른 지표는
    문턱 근처도 가지 않았다(surge 1.026 / 문턱 1.6, divergence 0.042 / 문턱 6.0).

    ⚠️ **계산이 틀린 것이 아니다.** ``dispersion = 1 - ‖평균 단위벡터‖`` 라
    사람들이 여러 방향으로 가면 1 에 가까워진다. **교차로는 본래 흐름이
    갈린다** — 문턱 0.6 이 교차로에 맞지 않는 것이다.

    ★ **등급이 늘 같으면 그 등급은 정보가 아니다.** 관제요원이 「지금 평소와
    다른가」에 답할 수 없다. 그래서 쏠림을 숫자로 드러낸다.

    ⚠️ 이 함수는 **판정을 바꾸지 않는다.** 문턱 조정은 지점 특성을 아는
    사람이 할 일이라, 여기서는 **사실만 보여 준다.**
    """
    try:
        from datetime import timedelta

        from sqlalchemy import func, select
        since = _now() - timedelta(days=max(days, 1))
        stmt = (select(CrowdObservation.severity,
                       func.count(CrowdObservation.id))
                .where(CrowdObservation.observed_at >= since,
                       CrowdObservation.failed.is_(False))
                .group_by(CrowdObservation.severity))
        if camera_id:
            stmt = stmt.where(CrowdObservation.camera_id == camera_id)
        rows = db.execute(stmt).all()
    except Exception:  # noqa: BLE001
        log.debug("등급 쏠림 조회 실패 camera_id=%s", camera_id, exc_info=True)
        return None

    counts = {int(sev or 0): int(n or 0) for sev, n in rows}
    total = sum(counts.values())
    if total <= 0:
        # 관측이 없으면 쏠림을 말하지 않는다. 0/0 을 「고르다」로 답하면
        # **아무것도 안 봤는데 정상인 것처럼** 보인다.
        return {"days": days, "camera_id": camera_id, "total": 0,
                "counts": counts, "top_severity": None, "top_ratio": None}
    top_sev = max(counts, key=lambda k: counts[k])
    return {"days": days, "camera_id": camera_id, "total": total,
            "counts": counts, "top_severity": top_sev,
            "top_ratio": round(counts[top_sev] / total, 4)}
