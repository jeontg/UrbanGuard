"""노면 점검 이력의 영구 보관 (road_inspections).

왜 필요한가
    지점별 최신 결과는 ``road/results.py`` 의 메모리에 있는데 **재시작하면
    사라집니다.** 그런데 보수 우선순위를 정하려면 「손상 4건」이 어제도
    4건이었는지 0건에서 늘어난 것인지를 알아야 합니다.

``events`` 로 대신할 수 없는 이유
    이벤트는 **손상이 잡혔을 때만** 생깁니다. 「봤는데 아무것도 없었다」와
    「분석에 실패했다」는 남지 않는데, 점검 이력에서는 그 둘이 핵심입니다.
    프레임 0장인 관측을 「이상 없음」으로 오해하면 못 본 구간을 점검 완료로
    처리하게 됩니다.

⚠️ 이 모듈의 실패는 절대 호출자를 막지 않습니다
    관제 분석이 DB 때문에 멈추면 안 됩니다. DB가 죽어 있어도 메모리 이력과
    화면은 그대로 동작해야 하므로, 저장은 실패를 삼키고 조회는 ``None`` 을
    돌려 호출자가 메모리로 내려가게 합니다.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

log = logging.getLogger("urbanguard.road_history")

# 지점당 보관 상한. 넘으면 오래된 것부터 지웁니다 — 파기 정책이 정해지기
# 전까지의 안전장치이며, 무한히 쌓여 DB가 부풀지 않게 합니다.
KEEP_PER_CAMERA = int(os.environ.get("URBANGUARD_ROAD_HISTORY_KEEP", "500"))
# 정리를 매번 하지 않는다 — 상한의 20% 만큼 여유를 두고 몰아서 지웁니다.
TRIM_SLACK = max(KEEP_PER_CAMERA // 5, 20)

_lock = threading.Lock()
_available: bool | None = None      # None=아직 확인 안 함


def _session():
    from .db import get_session
    return get_session()


def available() -> bool:
    """이력 테이블을 실제로 쓸 수 있는가.

    한 번 확인하고 결과를 기억합니다 — 화면이 10초마다 묻는데 그때마다
    DB를 두드릴 이유가 없습니다.
    """
    global _available
    if _available is not None:
        return _available
    with _lock:
        if _available is not None:
            return _available
        try:
            from sqlalchemy import select

            from .models import RoadInspection
            db = _session()
            try:
                db.execute(select(RoadInspection.id).limit(1))
                _available = True
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            log.info("점검 이력 테이블을 쓸 수 없습니다 — 메모리 이력만 씁니다",
                     exc_info=True)
            _available = False
    return _available


def reset_cache() -> None:
    """테스트용 — 사용 가능 여부를 다시 확인하게 한다."""
    global _available
    with _lock:
        _available = None


def _parse_at(value) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def save(camera_id: str, camera_name: str, entry: dict) -> bool:
    """관측 1건을 남긴다. 실패하면 조용히 False."""
    if not camera_id or not available():
        return False
    db = None
    try:
        from .models import RoadInspection
        db = _session()
        db.add(RoadInspection(
            camera_id=camera_id[:64],
            camera_name=(camera_name or "")[:120],
            grade=entry.get("grade"),
            defect_count=int(entry.get("defect_count") or 0),
            frames_analyzed=int(entry.get("frames_analyzed") or 0),
            failed=bool(entry.get("failed")),
            source=(entry.get("source") or "")[:24],
            # 구간 길이를 보정한 지점에서만 값이 있다. 관측 당시의 값을
            # 그대로 남긴다 — 나중에 구간 길이를 고쳐도 과거는 그대로여야 한다.
            per_100m=entry.get("per_100m"),
            note=(entry.get("note") or "")[:2000],
            analyzed_at=_parse_at(entry.get("analyzed_at")),
        ))
        db.commit()
        _trim(db, camera_id)
        return True
    except Exception:  # noqa: BLE001
        log.warning("점검 이력을 남기지 못했습니다 camera=%s", camera_id,
                    exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return False
    finally:
        if db is not None:
            db.close()


def _trim(db, camera_id: str) -> None:
    """지점당 보관 상한을 넘으면 오래된 것부터 지운다."""
    try:
        from sqlalchemy import func, select

        from .models import RoadInspection
        total = db.scalar(select(func.count(RoadInspection.id))
                          .where(RoadInspection.camera_id == camera_id)) or 0
        if total <= KEEP_PER_CAMERA + TRIM_SLACK:
            return
        keep_ids = db.scalars(
            select(RoadInspection.id)
            .where(RoadInspection.camera_id == camera_id)
            # ⚠️ 시각 하나로 정렬하면 **같은 초의 관측 순서가 보장되지
            #    않는다.** 인파 이력에서 실제로 뒤집힌 것을 확인해
            #    함께 고쳤다(2026-08-20 전체 점검). `id` 는 단조 증가한다.
            .order_by(RoadInspection.analyzed_at.desc(),
                      RoadInspection.id.desc())
            .limit(KEEP_PER_CAMERA)).all()
        if not keep_ids:
            return
        db.query(RoadInspection).filter(
            RoadInspection.camera_id == camera_id,
            ~RoadInspection.id.in_(keep_ids)).delete(synchronize_session=False)
        db.commit()
        log.info("점검 이력 정리 — %s (%d건 → %d건)", camera_id, total,
                 KEEP_PER_CAMERA)
    except Exception:  # noqa: BLE001
        log.debug("점검 이력 정리 실패 camera=%s", camera_id, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def recent(camera_id: str, limit: int) -> list[dict] | None:
    """최근 이력(오래된 것부터). 쓸 수 없으면 ``None`` 을 돌려 폴백시킨다.

    ``None`` 과 빈 목록을 구분하는 이유 — 빈 목록은 「관측한 적이 없다」이고
    ``None`` 은 「DB를 못 읽었다」입니다. 호출자가 메모리로 내려갈지 판단해야
    하므로 섞으면 안 됩니다.
    """
    if not camera_id or not available():
        return None
    db = None
    try:
        from sqlalchemy import select

        from .models import RoadInspection
        db = _session()
        rows = db.scalars(
            select(RoadInspection)
            .where(RoadInspection.camera_id == camera_id)
            # ⚠️ 시각 하나로 정렬하면 **같은 초의 관측 순서가 보장되지
            #    않는다.** 인파 이력에서 실제로 뒤집힌 것을 확인해
            #    함께 고쳤다(2026-08-20 전체 점검). `id` 는 단조 증가한다.
            .order_by(RoadInspection.analyzed_at.desc(),
                      RoadInspection.id.desc())
            .limit(max(limit, 1))).all()
        out = [{
            "analyzed_at": r.analyzed_at.isoformat() if r.analyzed_at else None,
            "defect_count": r.defect_count,
            "grade": r.grade,
            "frames_analyzed": r.frames_analyzed,
            "failed": r.failed,
            "source": r.source,
            "per_100m": r.per_100m,
        } for r in rows]
        out.reverse()          # 호출부 규약: 오래된 것부터
        return out
    except Exception:  # noqa: BLE001
        log.warning("점검 이력을 읽지 못했습니다 camera=%s", camera_id,
                    exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()


def latest_by_camera(camera_ids: list[str]) -> dict[str, dict] | None:
    """주어진 카메라들의 **가장 최근 관측 1건씩**. 쓸 수 없으면 ``None``.

    ⚠️ 2026-08-31 — API 게이트웨이 Phase 2(노면관리 서비스 분리) 신설.
    ``road/results.py``의 지점별 최신 결과(``_results``)는 순수 메모리라
    ``road_service.py``(별도 프로세스)만 갖고 있다 — platform-shell의
    홈 화면(``_road_summary()``)이 그 프로세스 안의 딕셔너리를 직접
    참조할 수 없다(2026-08-30 이전에는 같은 프로세스라 문제가 없었다).
    이 함수가 그 대신 DB(``road_inspections``, 이미 실시간 관측마다
    저장되고 있었다)에서 카메라별 최신 1건을 모아 온다 — 인파 도메인의
    ``_crowd_summary()``가 ``CrowdObservation``을 직접 읽는 것과 완전히
    같은 원칙("홈 화면은 도메인 서비스 프로세스 상태에 안 걸리게").
    """
    if not camera_ids or not available():
        return None
    db = None
    try:
        from sqlalchemy import func, select

        from .models import RoadInspection
        db = _session()
        # 카메라별 최신 id(단조 증가 — 위 recent()와 같은 정렬 근거)를 먼저
        # 뽑고, 그 id들로 실제 행을 가져온다.
        latest_ids_subq = (
            select(func.max(RoadInspection.id))
            .where(RoadInspection.camera_id.in_(camera_ids))
            .group_by(RoadInspection.camera_id)
        )
        rows = db.scalars(
            select(RoadInspection).where(RoadInspection.id.in_(latest_ids_subq))
        ).all()
        return {r.camera_id: {
            "analyzed_at": r.analyzed_at.isoformat() if r.analyzed_at else None,
            "defect_count": r.defect_count,
            "grade": r.grade,
            "frames_analyzed": r.frames_analyzed,
            "failed": r.failed,
            "source": r.source,
            "per_100m": r.per_100m,
        } for r in rows}
    except Exception:  # noqa: BLE001
        log.warning("카메라별 최신 점검 이력을 읽지 못했습니다", exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()


def detection_stats(camera_id: str = "", *, days: int = 30) -> dict | None:
    """관측 대비 **실제로 무언가를 찾은 비율**. 못 읽으면 ``None``.

    ★ **왜 필요한가 (2026-08-19 전수조사)**

    실측해 보니 노면 관측 **687회 중 680회(99.0%)가 탐지 0건**이었다. 화면에는
    「부산 CCTV에서 실사용 수준이 아니다」라는 **정성적 경고**만 있었고, 그것이
    지금도 사실인지 확인할 숫자가 어디에도 없었다.

    ⚠️ **경고문만 있고 숫자가 없으면 두 가지가 안 된다.**

    1. 관제요원이 「그래서 지금 얼마나 못 찾는가」를 모른다
    2. 모델을 바꿔도 **나아졌는지 증명할 수단이 없다**

    그래서 관측 이력에서 직접 센다. 별도 표를 만들지 않는다 —
    ``road_inspections`` 에 이미 다 들어 있다.

    ⚠️ **실패한 관측(``failed``)은 분모에서 뺀다.** 스트림이 끊겨 아무것도 못
    본 것을 「못 찾았다」로 세면, 모델이 실제보다 나빠 보인다. 대신 그 횟수를
    ``failed`` 로 따로 돌려준다 — 감추면 안 되는 값이다.
    """
    if not available():
        return None
    db = None
    try:
        from datetime import timedelta

        from sqlalchemy import case, func, select

        from .models import RoadInspection
        db = _session()
        since = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
        # 탐지 0건이면서 **실패가 아닌** 관측만 센다.
        zero_case = case(
            ((RoadInspection.failed.is_(False))
             & (RoadInspection.defect_count == 0), 1),
            else_=0)
        stmt = select(
            func.count(RoadInspection.id),
            func.sum(zero_case),
            func.sum(case((RoadInspection.failed.is_(True), 1), else_=0)),
        ).where(RoadInspection.analyzed_at >= since)
        if camera_id:
            stmt = stmt.where(RoadInspection.camera_id == camera_id)
        total, zero, failed = db.execute(stmt).one()
        total = int(total or 0)
        zero = int(zero or 0)
        failed = int(failed or 0)
        analyzed = total - failed
        # 분석이 한 번도 성립하지 않았으면 비율을 말하지 않는다. 0/0 을
        # 「0%」로 답하면 **아무것도 안 했는데 다 찾은 것처럼** 보인다.
        rate = None if analyzed <= 0 else round(1.0 - (zero / analyzed), 4)
        return {"days": days, "camera_id": camera_id,
                "total": total, "analyzed": analyzed, "failed": failed,
                "zero_detection": zero, "detection_rate": rate}
    except Exception:  # noqa: BLE001
        log.debug("탐지율 조회 실패 camera_id=%s", camera_id, exc_info=True)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
