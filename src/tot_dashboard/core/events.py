"""이벤트 생성·조회·조치 (S-02 / S-03).

탐지 파이프라인은 「지금 이 지점이 몇 등급인가」만 알려 준다. 그 연속된 관측을
**사건 단위로 묶는 것**이 이 모듈의 역할이다. 같은 지점의 위험이 이어지는 동안
이벤트가 계속 새로 생기면 큐가 쓸모없어지므로, 열려 있는 이벤트가 있으면
등급과 관측값만 갱신한다.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import vocabulary as V
from .models import Event, EventAction

log = logging.getLogger("urbanguard.events")

# 상태
OPEN = "open"            # 미처리
IN_PROGRESS = "progress"  # 처리중
CLOSED = "closed"        # 완료
# ⚠️ 2026-09-02 신설(이벤트 자동 보류 정책) — **종결(CLOSED)이 아니다.**
# "위험이 해소됐다"는 시스템의 판단을 뜻하지 않는다 — 재탐지가 오래
# 끊겨 더 볼 필요가 줄었으니 사람이 확인해 정말 닫을지 재개할지 판단해
# 달라는 중간 상태다. CLOSED를 그대로 썼다면 `core/feedback.py::
# pending_events()`(S-07 AI 학습 피드백 대기열, `status=="closed"`인
# 것만 뽑음)에 사람이 실제로 조치·판단해서 닫은 이벤트와 구분 없이
# 섞여 들어갔을 것이다 — 그래서 제3의 상태로 둔다.
AUTO_HELD = "auto_held"  # 자동 보류
STATUS_LABELS = {OPEN: "미처리", IN_PROGRESS: "처리중", CLOSED: "완료",
                 AUTO_HELD: "자동 보류"}
# "정말 미해결"의 정의 — 자동 보류 신설로 바뀌지 않는다. 자동 보류는
# 그 밖의 제3의 상태다(위 AUTO_HELD 주석 참고).
ACTIVE = (OPEN, IN_PROGRESS)

# 위험등급. 침수 도메인의 4단계를 공통 척도로 쓴다.
LEVELS = ("관심", "주의", "경계", "심각")
LEVEL_RANK = {lv: i for i, lv in enumerate(LEVELS)}
# 이벤트로 올릴 최소 등급 — 「관심」까지 이벤트가 되면 큐가 평상시에도 가득 찬다.
EVENT_THRESHOLD = "주의"

# 조치 코드
ACT_DETECTED = "detected"          # 자동 탐지 (시스템이 남김)
ACT_LEVEL_UP = "level_up"          # 등급 상승
ACT_ACKNOWLEDGE = "acknowledge"    # 확인 (→ 처리중)
ACT_MEMO = "memo"                  # 조치 기록
ACT_NOTIFY_DEPT = "notify_dept"    # 부서 통보
ACT_NOTIFY_AGENCY = "notify_agency"  # 112·119·재난상황실 통보
ACT_FALSE_POSITIVE = "false_positive"  # 오탐 신고
ACT_CLOSE = "close"                # 종결
ACT_AUTO_HOLD = "auto_hold"        # 자동 보류(재탐지 없음, 2026-09-02 신설)

ACTION_LABELS = {
    ACT_DETECTED: "자동 탐지", ACT_LEVEL_UP: "등급 상승",
    ACT_ACKNOWLEDGE: "확인", ACT_MEMO: "조치 기록",
    ACT_NOTIFY_DEPT: "부서 통보", ACT_NOTIFY_AGENCY: "기관 통보(112·119·재난)",
    ACT_FALSE_POSITIVE: "오탐 신고", ACT_CLOSE: "종결",
    ACT_AUTO_HOLD: "자동 보류(재탐지 없음)",
}


# 탐지가 이벤트로 올라간 순간 부르는 훅. 증거 수집(S-88)이 여기에 붙는다.
#
# **코어가 서비스를 임포트하지 않기 위해** 콜백으로 둔다. 훅에서 난 예외는
# 삼킨다 — 증거를 못 남겼다고 이벤트 기록이 실패하면 본말이 전도된다.
_detection_hook = None


def set_detection_hook(fn) -> None:
    global _detection_hook
    _detection_hook = fn


def _fire_hook(ev, *, is_new: bool) -> None:
    if _detection_hook is None:
        return
    try:
        _detection_hook(ev, is_new=is_new)
    except Exception as e:  # noqa: BLE001
        log.warning("탐지 훅 실패 event=%s: %s", getattr(ev, "id", "?"), str(e)[:160])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def rank(level: str) -> int:
    return LEVEL_RANK.get(level or "", -1)


def is_reportable(level: str) -> bool:
    return rank(level) >= rank(EVENT_THRESHOLD)


def add_action(db: Session, event: Event, action: str, *, user=None,
               memo: str = "") -> EventAction:
    row = EventAction(event_id=event.id, action=action, memo=memo,
                      user_id=getattr(user, "id", None),
                      login_id=getattr(user, "login_id", "") or "")
    db.add(row)
    return row


def open_event_for(db: Session, domain: str, block_id: str,
                   hazard_type_code: str = "") -> Event | None:
    """열려 있는 이벤트를 찾는다.

    ``hazard_type_code`` 를 주면 그 유형까지 맞아야 같은 이벤트로 본다.
    빈 문자열(기본값)이면 (도메인, 지점)만 본다. ``record_detection()``
    이 ``split_by_hazard_type=True`` 일 때만 정규화된 코드를 넘기고,
    그 외에는 항상 빈 문자열을 넘긴다 — 그 함수의 docstring 참고.
    """
    q = select(Event).where(Event.domain == domain, Event.block_id == block_id,
                            Event.status.in_(ACTIVE))
    if hazard_type_code:
        q = q.where(Event.hazard_type_code == hazard_type_code)
    return db.scalar(q.order_by(Event.detected_at.desc(), Event.id.desc()))


def record_detection(db: Session, *, domain: str, block_id: str,
                     place_name: str, level: str, event_type: str = "",
                     confidence: float | None = None,
                     detail: dict | None = None,
                     hazard_type_code: str = "",
                     split_by_hazard_type: bool = False) -> Event | None:
    """탐지 결과를 이벤트로 반영한다.

    - 열려 있는 이벤트가 없고 등급이 기준 이상이면 새로 만든다
    - 열려 있는 이벤트가 있으면 등급·관측값만 갱신한다
    - 등급이 올라가면 이력을 남긴다. 내려가도 **자동으로 닫지 않는다** —
      종결은 사람이 확인하고 판단할 일이다

    반환값은 만들어졌거나 갱신된 이벤트. 아무 일도 없으면 None.

    ★ 유형 어휘와 잇는다. 예전에는 이 칸을 **채우는 곳이 없어** 늘 비어
    있었고, 유형 기준 통계·필터·SOP 연결이 전부 빈손이었다(2026-08-19).
    ``event_type`` 은 「침수·교통위험」 같은 **화면 문구**라 코드로 쓸 수
    없다. 그래서 도메인에서 대분류를 도출한다.

    ★ 2026-08-26 — ``split_by_hazard_type=True`` 를 주면 유형까지 같아야
    같은 이벤트로 본다(교통 돌발상황 확장 — 같은 카메라에서 강우정체·
    역주행·보행자가 동시에 열려 있어야 한다). **기본값은 False다** —
    (도메인, 지점)만으로 찾는 기존 동작 그대로다.

    ⚠️ 기본값을 True로 바꾸면 안 된다. 세분류가 이미 붙은 이벤트에 나중에
    덜 구체적인 탐지(대분류로 정규화됨)가 들어오면 **다른 이벤트로
    보여 중복이 생긴다**(`tests/core/test_event_hazard_type.py`의
    ``test_기존_이벤트를_갱신할_때는_건드리지_않는다`` 가 이 계약을
    지킨다). TWR_* 판정(강우 정체→정지차량 다발→교통마비)도 **한 상황이
    악화되는 과정**이라 여전히 하나로 합쳐야 하므로 기본값을 쓴다 —
    유형을 실제로 나눠야 하는 것은 서로 **동시에 일어날 수 있는 독립
    사건**(보행자·역주행·사고 의심)뿐이다.
    """
    norm_code = V.normalize_hazard_code(hazard_type_code, domain) or ""
    lookup_code = norm_code if split_by_hazard_type else ""
    existing = open_event_for(db, domain, block_id, lookup_code)

    if existing is None:
        if not is_reportable(level):
            return None
        now = _now()
        ev = Event(domain=domain, block_id=block_id, place_name=place_name,
                   event_type=event_type or domain, level=level,
                   peak_level=level, status=OPEN, confidence=confidence,
                   detail=detail or {}, last_detected_at=now,
                   hazard_type_code=norm_code or None)
        db.add(ev)
        db.flush()
        add_action(db, ev, ACT_DETECTED,
                   memo=f"{place_name} {level} 등급 자동 탐지")
        log.info("이벤트 생성 domain=%s block=%s level=%s", domain, block_id, level)
        _fire_hook(ev, is_new=True)
        return ev

    before = existing.level
    existing.level = level
    existing.confidence = confidence
    if detail:
        existing.detail = detail
    existing.updated_at = _now()
    # ⚠️ 2026-09-02 신설(이벤트 자동 보류 정책) — `updated_at`과 달리
    # **임계등급(`EVENT_THRESHOLD`) 이상으로 재탐지됐을 때만** 갱신한다.
    # "관심" 같은 낮은 등급으로의 재탐지나 20초 하트비트성 갱신까지
    # "재탐지"로 치면 자동 보류 판정이 무의미해진다(core/events.py 머리말
    # §자동 보류 설계 참고).
    if is_reportable(level):
        existing.last_detected_at = _now()
    if rank(level) > rank(existing.peak_level):
        existing.peak_level = level
        add_action(db, existing, ACT_LEVEL_UP, memo=f"{before} → {level}")
        # 등급이 오르면 그 순간도 증거로 남긴다 — 「주의로 시작해 심각까지
        # 갔다」가 사후 검토의 핵심이다.
        _fire_hook(existing, is_new=False)
        log.info("이벤트 등급 상승 id=%s %s→%s", existing.id, before, level)
    return existing


def acknowledge(db: Session, event: Event, user, memo: str = "") -> None:
    """확인 — 미처리에서 처리중으로. 담당자가 정해진다."""
    if event.status == OPEN:
        event.status = IN_PROGRESS
    if event.assignee_id is None:
        event.assignee_id = getattr(user, "id", None)
    add_action(db, event, ACT_ACKNOWLEDGE, user=user, memo=memo)


def close(db: Session, event: Event, user, memo: str = "") -> None:
    event.status = CLOSED
    event.closed_at = _now()
    event.closed_by = getattr(user, "id", None)
    add_action(db, event, ACT_CLOSE, user=user, memo=memo)


def mark_false_positive(db: Session, event: Event, user, memo: str = "") -> None:
    """오탐 신고. 모델은 실환경 미검증이라, 이 기록이 재학습 데이터가 된다."""
    event.false_positive = True
    event.status = CLOSED
    event.closed_at = _now()
    event.closed_by = getattr(user, "id", None)
    add_action(db, event, ACT_FALSE_POSITIVE, user=user, memo=memo)


def list_events(db: Session, *, status: str = "", domain: str = "",
                allowed_domains: set[str] | None = None,
                limit: int = 200) -> list[Event]:
    stmt = select(Event)
    if status == "active":
        stmt = stmt.where(Event.status.in_(ACTIVE))
    elif status:
        stmt = stmt.where(Event.status == status)
    if domain:
        stmt = stmt.where(Event.domain == domain)
    if allowed_domains is not None:
        stmt = stmt.where(Event.domain.in_(allowed_domains or {"__none__"}))
    # 미처리를 먼저, 그 안에서는 오래된 것을 먼저 — 방치를 막기 위해서다.
    stmt = stmt.order_by(Event.status, Event.detected_at.desc(), Event.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def counts(db: Session, allowed_domains: set[str] | None = None) -> dict[str, int]:
    out = {OPEN: 0, IN_PROGRESS: 0, CLOSED: 0, AUTO_HELD: 0}
    stmt = select(Event.status)
    if allowed_domains is not None:
        stmt = stmt.where(Event.domain.in_(allowed_domains or {"__none__"}))
    for s in db.scalars(stmt).all():
        if s in out:
            out[s] += 1
    return out


def auto_hold_stale(db: Session, *, threshold_hours: float) -> list[Event]:
    """`threshold_hours` 시간 넘게 재탐지(임계등급 이상)가 없는 미해결
    이벤트를 자동 보류로 옮긴다.

    ⚠️ **종결이 아니다** — 위 AUTO_HELD 주석 참고. 사람이 다시 확인해
    정말 닫을지(`close()`) 재개할지 판단해야 한다. 재탐지가 다시 오면
    `open_event_for()`가 `ACTIVE`(이 함수가 옮긴 `AUTO_HELD`는 포함 안
    됨)만 찾으므로 **새 이벤트가 열린다** — `CLOSED` 이벤트에 재탐지가
    오면 새 이벤트가 열리는 기존 동작과 같은 패턴이다.

    `func.coalesce`로 `last_detected_at`이 비어 있는 행(마이그레이션
    직후 등)은 `detected_at`으로 대신 판단한다 — 방어적 처리일 뿐,
    정상 배포에서는 마이그레이션이 이미 채워 둔다.
    """
    cutoff = _now() - timedelta(hours=threshold_hours)
    stmt = select(Event).where(
        Event.status.in_(ACTIVE),
        func.coalesce(Event.last_detected_at, Event.detected_at) < cutoff)
    moved = []
    for ev in db.scalars(stmt):
        ev.status = AUTO_HELD
        add_action(db, ev, ACT_AUTO_HOLD,
                   memo=f"{threshold_hours:.0f}시간 이상 재탐지 없음 — 자동 보류")
        log.info("이벤트 자동 보류 id=%s domain=%s block=%s",
                ev.id, ev.domain, ev.block_id)
        moved.append(ev)
    return moved


# 자동 보류 감시 스레드 확인 주기. 기준(threshold_hours)이 시간 단위라
# 자주 돌 필요가 없다 — `core/video_quality.py::start_scanner()`·
# `core/ffmpeg_relay.py::start_watchdog()`와 같은 자리, 같은 뼈대.
_AUTO_HOLD_INTERVAL_SEC = 1800  # 30분


def _auto_hold_loop() -> None:
    from . import audit as ug_audit
    from . import settings as ug_settings
    from .db import get_session

    print("[events] 자동 보류 감시 스레드 시작")
    while True:
        try:
            db = get_session()
            try:
                if ug_settings.event_auto_hold_enabled(db):
                    hours = ug_settings.event_auto_hold_hours(db)
                    moved = auto_hold_stale(db, threshold_hours=hours)
                    if moved:
                        for ev in moved:
                            ug_audit.record(
                                db, action=ug_audit.EVENT_AUTO_HOLD,
                                login_id="system",
                                target=f"이벤트 #{ev.id} {ev.place_name or ev.block_id}",
                                after={"domain": ev.domain, "hours": hours})
                        db.commit()
                        print(f"[events] 자동 보류 {len(moved)}건 "
                              f"(기준 {hours:.0f}시간)")
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            # 이 루프가 죽어도 서비스 자체는 떠 있어야 한다 — 이
            # 저장소의 반복된 원칙.
            print(f"[events] 자동 보류 루프 오류(무시): {str(e)[:160]}")
        time.sleep(_AUTO_HOLD_INTERVAL_SEC)


def start_auto_hold_watchdog() -> None:
    """서비스 기동 시 1회 호출한다(main.py lifespan, ffmpeg_relay.
    start_watchdog()·video_quality.start_scanner()과 같은 자리).
    platform-shell 하나에서만 돈다 — 이벤트는 도메인 프로세스가 아니라
    공유 DB 하나에 모이므로 5개 서비스 전부에서 돌릴 이유가 없다."""
    threading.Thread(target=_auto_hold_loop, daemon=True,
                     name="event-auto-hold").start()


def elapsed_text(ev: Event) -> str:
    """경과 시간. 큐에서 방치를 눈에 띄게 하려고 함께 보여 준다."""
    start = ev.detected_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = ev.closed_at or _now()
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    mins = int((end - start).total_seconds() // 60)
    if mins < 1:
        return "방금"
    if mins < 60:
        return f"{mins}분"
    if mins < 60 * 24:
        return f"{mins // 60}시간 {mins % 60}분"
    return f"{mins // (60 * 24)}일"
