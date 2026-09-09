"""카메라별 「지금」 판정의 크로스 프로세스 조회 (API 게이트웨이 Phase 4).

왜 필요한가
    Phase 4로 침수·교통위험이 각자 별도 프로세스(flood-service·
    traffic-service)가 되면, platform-shell의 홈 화면(``_flood_summary()``·
    ``_traffic_summary()``)은 더 이상 그 프로세스들의 인메모리 ``RiskStore``를
    직접 읽을 수 없다 — 인파(``core/models.py::CrowdObservation``)·노면
    (:func:`.road_history.latest_by_camera`)이 이미 겪어 해결한 것과 같은
    문제다.

이 모듈이 하는 일은 딱 둘
    - :func:`upsert` — 각 서비스가 자기 판정 결과를 카메라·도메인당
      **행 하나로 계속 덮어쓴다**(1초 주기 정도가 적당 — 5fps 전체를 실어
      나를 필요는 없다, 홈 화면은 초 단위 신선도면 충분하다).
    - :func:`latest_by_camera` — 다른 프로세스가 그 최신 상태를 즉시 읽는다.

⚠️ 이 모듈의 실패는 절대 호출자를 막지 않는다
    관제 판정이 이 표 때문에 멈추면 안 된다. DB가 죽어 있어도 그 서비스
    자신의 API·화면은 그대로 동작해야 하므로, 저장은 실패를 삼키고 조회는
    ``None`` 을 돌려 호출자가 "관측 없음"으로 내려가게 한다.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("urbanguard.live_state")

_lock = threading.Lock()
_available: bool | None = None      # None=아직 확인 안 함


def _session():
    from .db import get_session
    return get_session()


def available() -> bool:
    """이 표를 실제로 쓸 수 있는가. 한 번 확인하고 기억한다(road_history와 동일)."""
    global _available
    if _available is not None:
        return _available
    with _lock:
        if _available is not None:
            return _available
        try:
            from sqlalchemy import select

            from .models import LiveDetectionState
            db = _session()
            try:
                db.execute(select(LiveDetectionState.id).limit(1))
                _available = True
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            log.info("live_detection_state 표를 쓸 수 없습니다 — 홈 화면 "
                     "교차조회를 건너뜁니다", exc_info=True)
            _available = False
    return _available


def reset_cache() -> None:
    """테스트용 — 사용 가능 여부를 다시 확인하게 한다."""
    global _available
    with _lock:
        _available = None


def upsert(camera_id: str, domain: str, *, level: str = "",
          is_available: bool = True) -> bool:
    """카메라·도메인 한 쌍의 「지금」 상태를 덮어쓴다. 실패하면 조용히 False.

    :param level: 관심/주의/경계/심각 중 하나, 또는 판정이 없으면 "".
    :param is_available: 이번 틱에 실제로 판정이 돌았는가(각 도메인의
        water_available/traffic_enabled에 해당). 저장 자체는 항상 성공시키되,
        이 값으로 조회 쪽(:func:`latest_by_camera`)이 "관측 없음"을 가릴 수
        있게 한다.
    """
    cam = (camera_id or "").strip()
    dom = (domain or "").strip()
    if not cam or not dom or not available():
        return False
    db = None
    try:
        from sqlalchemy import select

        from .models import LiveDetectionState
        db = _session()
        row = db.scalars(
            select(LiveDetectionState)
            .where(LiveDetectionState.camera_id == cam,
                   LiveDetectionState.domain == dom)).first()
        if row is None:
            row = LiveDetectionState(camera_id=cam[:64], domain=dom[:16])
            db.add(row)
        row.level = (level or "")[:8]
        row.available = bool(is_available)
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        log.warning("live_detection_state 갱신 실패 camera=%s domain=%s",
                    camera_id, domain, exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return False
    finally:
        if db is not None:
            db.close()


def latest_by_camera(camera_ids: list[str], domain: str) -> dict[str, dict] | None:
    """주어진 카메라들의 「지금」 상태(도메인 하나). 쓸 수 없으면 ``None``.

    반환값은 ``{camera_id: {"level": str, "available": bool, "updated_at": str}}``.
    ``None``과 빈 dict를 구분한다 — ``None``은 "표를 못 읽었다"(호출자가
    다른 폴백으로 내려가야 함), 빈 dict는 "표는 읽었는데 해당 카메라의
    관측이 아직 없다"이다(road_history.latest_by_camera와 같은 규약).
    """
    if not camera_ids or not domain or not available():
        return None
    db = None
    try:
        from sqlalchemy import select

        from .models import LiveDetectionState
        db = _session()
        rows = db.scalars(
            select(LiveDetectionState)
            .where(LiveDetectionState.camera_id.in_(camera_ids),
                   LiveDetectionState.domain == domain)).all()
        return {r.camera_id: {
            "level": r.level or "",
            "available": bool(r.available),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        } for r in rows}
    except Exception:  # noqa: BLE001
        log.warning("live_detection_state 조회 실패 domain=%s", domain,
                    exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()
