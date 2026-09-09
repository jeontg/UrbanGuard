"""탐지 결과를 이벤트로 옮기는 동기화 스레드.

탐지 파이프라인(`RiskStore`, 인파 분석기)은 「지금 이 지점이 몇 등급인가」라는
**현재 상태**만 들고 있다. 관제요원이 다루는 것은 그 연속된 관측을 묶은
**사건**이므로, 주기적으로 읽어 이벤트로 반영한다.

도메인 패키지를 고치지 않고 서비스 계층에서 읽어 가는 방식을 택했다.
탐지 코드는 이벤트의 존재를 몰라도 되고, 이벤트 정의가 바뀌어도 탐지 코드는
영향받지 않는다.
"""
from __future__ import annotations

import logging
import threading
import time

from ..core import events
from ..core import traffic_history
from ..core.db import get_session
from ..core.roles import Domain
from ..traffic_weather.knowledge.ontology import TWR_TO_HAZARD_TYPE

log = logging.getLogger("urbanguard.event_sync")

INTERVAL_SEC = 20.0


# ★ 2026-08-21 flood/traffic 도메인 분리 — 침수 등급 → 이벤트 등급
#
#   RiskEngine 은 5등급(매우낮음~매우높음)을 내는데, 상황판 이벤트는
#   행안부 4단계(관심·주의·경계·심각)를 쓴다. 두 어휘가 달라 매핑이 필요하다.
#
#   경계(4)에서 끊은 것은 알림 기준(`runner._notify_flood_risk` 의
#   ``min_grade=4``)과 맞추기 위해서다 — 알림은 갔는데 이벤트 등급은
#   「주의」로 남으면 관제요원이 둘을 대조할 때 어긋난다.
#
# ⚠️ 이 대응은 실환경에서 검증되지 않았다(RiskEngine 의 grade_bins 자체가
#   자체 판단값이다). 재난 담당부서 협의가 필요하다.
_FLOOD_GRADE_LEVEL = {1: "관심", 2: "관심", 3: "주의", 4: "경계", 5: "심각"}


def _sync_traffic(db, store) -> int:
    """교통위험 이벤트 — 강우 × 정체 × 정지차량 판정(TWR_*)에서 온다."""
    n = 0
    for snap in (store.all() or []):
        block_id = snap.get("block_id") or ""
        if not block_id:
            continue
        # ★ 2026-08-28 — 교통위험이 미지정인 카메라(침수만 상시)는 이력·
        # 이벤트를 만들지 않는다. `_sync_flood()`의 water_available 게이트와
        # 같은 이유다 — "판정을 안 했다"와 "판정했는데 정상이었다"는 다르다.
        # 이 값이 없기 전에는 침수만 상시로 켠 카메라도 매 20초 「정상」
        # 교통 관측 이력이 쌓이고 있었다(실사용 점검 중 발견,
        # docs/pending_tasks.md 2026-08-28 항목).
        if not snap.get("traffic_enabled", True):
            continue
        detail = {}
        # ⚠️ 여기 키 이름은 예전에 전부 틀려 있었다 — `rain_mm`/`water_ratio`/
        #   `vehicles` 로 찾는데 스냅샷에는 `rain_mm_h`/`water_area_ratio`/
        #   `n_vehicles` 로 들어 있어, **detail 이 한 번도 채워진 적이 없었다.**
        #   도메인을 나누며 함께 바로잡았다(2026-08-21).
        for key, label in (("rain_mm_h", "강수(mm/h)"), ("intensity", "강수 강도"),
                           ("n_vehicles", "차량 수"),
                           ("mean_speed_kmh", "평균속도(km/h)")):
            if snap.get(key) is not None:
                detail[label] = snap[key]
        # ★ 2026-08-26 — TWR_* 판정코드를 유형 어휘로 옮긴다. 예전에는
        #   여기서 hazard_type_code 를 아예 넘기지 않아, 위험유형 4종이
        #   한 번도 채워진 적이 없었다(`docs/202608260842/` 참고). 판정코드
        #   자체도 이벤트에 남는 곳이 없었으므로 detail 에 함께 적어 사후
        #   추적이 가능하게 한다.
        risk_code = snap.get("risk_code") or ""
        hazard_code = TWR_TO_HAZARD_TYPE.get(risk_code, "")
        if risk_code:
            detail["판정코드"] = risk_code
        # ★ 2026-08-22 — 관측 이력을 남긴다(미결 과제 7A-6).
        #   ``traffic_observations`` 표는 2026-08-21 에 만들어 뒀는데
        #   **기록하는 코드가 없어 한 건도 쌓이지 않았다.** 이벤트는 위험할
        #   때만 남으므로, 「봤는데 평온했다」가 빠지면 평상시 기준선을 만들
        #   수 없다(crowd 와 같은 이유). 이벤트 생성 여부와 **무관하게**
        #   매번 남기는 것이 핵심이다.
        try:
            traffic_history.record(
                db, camera_id=block_id,
                camera_name=snap.get("name") or block_id,
                snapshot=snap, source=snap.get("source_kind") or "")
        except Exception:  # noqa: BLE001
            # 이력 기록이 이벤트 생성을 막으면 안 된다 — 관제가 우선이다.
            log.exception("교통 관측 이력 기록 실패 camera=%s", block_id)

        ev = events.record_detection(
            db, domain=Domain.TRAFFIC.value, block_id=block_id,
            place_name=snap.get("name") or block_id,
            level=snap.get("level") or "",
            event_type="교통위험", detail=detail or None,
            hazard_type_code=hazard_code)
        if ev is not None:
            n += 1

        # ★ 2026-08-26 — 돌발상황(보행자·역주행·사고 의심). 위 TWR_* 판정과
        #   달리 **동시에 일어날 수 있는 독립 사건**이라 split_by_hazard_type
        #   로 각자 별도 이벤트가 되게 한다(`core/events.py` 참고 — 강우정체
        #   위에서 보행자가 지나가도 서로 덮어쓰면 안 된다).
        for inc in (snap.get("incidents") or []):
            inc_ev = events.record_detection(
                db, domain=Domain.TRAFFIC.value, block_id=block_id,
                place_name=snap.get("name") or block_id,
                level=inc.get("level") or "",
                event_type=inc.get("label") or "교통 돌발상황",
                confidence=inc.get("confidence"),
                detail={"근거": inc.get("evidence_text"),
                       "판정": inc.get("code")},
                hazard_type_code=inc.get("hazard_type_code") or "",
                split_by_hazard_type=True)
            if inc_ev is not None:
                n += 1
    return n


def _sync_flood(db, store) -> int:
    """침수 이벤트 — 물 세그멘테이션 위험도(RiskEngine)에서 온다.

    ★ 예전에는 이 함수 하나가 침수와 교통을 **한 이벤트로 합쳐** 올렸고,
    등급도 교통·기상 판정(`decision.level`)을 그대로 썼다. 그래서 관제요원이
    「이게 침수 때문인지 정체 때문인지」 구분할 수 없었다.
    """
    n = 0
    for snap in (store.all() or []):
        block_id = snap.get("block_id") or ""
        if not block_id:
            continue
        # 물 세그멘테이션이 안 돌면 침수 이벤트를 만들지 않는다 —
        # 「탐지 0건」과 「보지 못했다」는 다르다.
        if not snap.get("water_available"):
            continue
        level = _FLOOD_GRADE_LEVEL.get(snap.get("flood_risk_grade"), "")
        detail = {}
        for key, label in (("water_area_ratio", "물 비율"),
                           ("flood_risk_score", "침수 위험도"),
                           ("flood_depth_cm", "추정 침수심(cm)"),
                           ("vehicles_tire_in_water", "바퀴 침수 차량"),
                           ("persons_in_danger", "위험 보행자")):
            if snap.get(key) is not None:
                detail[label] = snap[key]
        ev = events.record_detection(
            db, domain=Domain.FLOOD.value, block_id=block_id,
            place_name=snap.get("name") or block_id, level=level,
            event_type="침수", detail=detail or None)
        if ev is not None:
            n += 1
    return n


# ★ 밀집도 이벤트 (2026-08-19 전수조사).
#
#   전수조사에서 인파 이벤트 11건이 **전부 배회·침입 경로**였다. 밀집도는
#   이력만 남기고 이벤트를 만들지 않아, **`severity 3`(군중급증위험) 5회가
#   이벤트가 되지 않았다.** 경남 SFR-008 은 「인파 밀집 감지」를 요구한다.
#
# ⚠️ **그런데 문턱대로 다 올리면 이벤트가 폭주한다.** 같은 조사에서 관측
#   670회 중 627회(93.6%)가 `severity 2` 였다 — 판정 문턱이 교차로 특성과
#   맞지 않아 「이동흐름혼란」이 상시 발동하기 때문이다.
#
# ★ 그래서 **`severity 3` 이상만** 올린다. 실측 5회뿐이라 폭주하지 않고,
#   쏠린 등급(2)은 제외해 **이벤트 큐가 무의미해지는 것을 막는다.**
#   문턱이 현장 보정되면 이 기준도 함께 손봐야 한다.
#
# ★ 2026-08-26: 문턱(몇 심각도부터 올릴지)은 관리자가 S-95 화면에서
#   조정할 수 있게 뺐다(core.settings.crowd_density_min_severity, 기본값
#   3 — 지금까지의 동작 그대로). 심각도→등급 **대응**은 여기 그대로 둔다 —
#   crowd 고유 분류(정상~패닉분산 0~4)를 상황판 4단계로 옮기는 변환표라
#   문턱과는 별개다.
_CROWD_SEVERITY_LEVEL = {0: "관심", 1: "관심", 2: "주의", 3: "경계", 4: "심각"}


def _crowd_density_event(db, camera_id: str, camera_name: str, d: dict) -> None:
    """밀집도 등급이 설정된 문턱 이상이면 이벤트로 올린다. 낮으면 아무것도
    하지 않는다."""
    if not isinstance(d, dict):
        return
    try:
        sev = int(d.get("severity") or 0)
    except (TypeError, ValueError):
        return
    from ..core import settings as ug_settings
    if sev < ug_settings.crowd_density_min_severity():
        return
    level = _CROWD_SEVERITY_LEVEL.get(sev)
    if not level:
        return
    events.record_detection(
        db, domain=Domain.CROWD.value, block_id=camera_id,
        place_name=camera_name or camera_id, level=level,
        event_type="인파관리",
        detail={"판정": d.get("risk_name") or d.get("risk_code") or "",
                "위험도 점수": d.get("risk_score"),
                "현재 인원": d.get("person_count"),
                "밀집지수": d.get("density_index"),
                # ⚠️ 판정 근거를 함께 남긴다 — 「경계」만 뜨고 왜인지 없으면
                #    관제요원이 확인할 것을 못 찾는다.
                "근거": ", ".join(d.get("drivers") or []) or "미상"})


def record_crowd_observation(camera_id: str, camera_name: str,
                             snap: dict | None) -> None:
    """인파 흐름 지표를 이력으로 남긴다.

    이벤트(:func:`record_crowd_snapshot`)와 **일부러 나눴다.** 이벤트는
    위험행동이 잡혔을 때만 생기는데, 기준선을 만들려면 **평온했던 관측도**
    남아야 한다. 「봤는데 아무 일 없었다」가 빠지면 급증을 가려낼 수 없다.

    실패는 삼킨다 — **이력 기록이 관제를 멈추면 안 된다.**
    """
    if not camera_id:
        return
    d = snap if isinstance(snap, dict) else {}
    db = None
    try:
        from ..core import crowd_history

        db = get_session()
        crowd_history.record(db, camera_id=camera_id, camera_name=camera_name,
                             snapshot=d,
                             # 프레임을 못 받아 사람이 0인 것과 실제로 0인
                             # 것은 다르다. 분석기가 준 값이 비었으면 실패로 본다.
                             failed=not bool(d))
        _crowd_density_event(db, camera_id, camera_name, d)
        db.commit()
    except Exception:  # noqa: BLE001
        log.exception("인파 관측 이력 기록 실패 camera=%s", camera_id)
        if db is not None:
            db.rollback()
    finally:
        if db is not None:
            db.close()


def record_crowd_snapshot(snap: dict) -> None:
    """인파 분석 스냅샷에서 위험행동이 잡히면 이벤트로 올린다.

    동기화 스레드가 아니라 **분석기를 이미 돌린 쪽**(`/api/crowd/live`)에서
    호출한다. 분석기의 ``step()`` 은 프레임을 진행시키는 호출이라 두 곳에서
    부르면 시간축이 어긋나기 때문이다.

    호출자의 응답을 막지 않도록 실패는 삼키고 로깅만 한다.
    """
    if not isinstance(snap, dict):
        return
    evs = snap.get("events") or []
    if not evs:
        return
    kinds = sorted({(e.get("eventType") or e.get("type") or "") for e in evs} - {""})
    db = None
    try:
        db = get_session()
        # 배회·침입이 잡히면 「주의」로 올린다. 등급 산정 기준은 현장
        # 캘리브레이션 후 조정해야 한다(설계서 1-1절 잔여 제약).
        ev = events.record_detection(
            db, domain=Domain.CROWD.value,
            block_id=snap.get("block_id") or "CROWD",
            place_name=snap.get("node_id") or snap.get("block_id") or "인파 감시구역",
            level="주의", event_type="인파관리",
            detail={"탐지 유형": ", ".join(kinds) or "미상",
                    "현재 인원": snap.get("people_count"),
                    "데이터 소스": (snap.get("source") or {}).get("person")
                    if isinstance(snap.get("source"), dict) else snap.get("source")})
        if ev is not None:
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("인파 이벤트 기록 실패")
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if db is not None:
            db.close()


def record_road_result(result: dict) -> None:
    """노면 분석 결과에서 손상이 잡히면 이벤트로 올린다.

    인파와 같은 이유로 **분석을 돌린 쪽**에서 직접 호출한다. 노면 분석은
    주기가 길어(분 단위) 동기화 스레드가 뒤늦게 읽을 상태 저장소가 없다.

    호출자를 막지 않도록 실패는 삼키고 로깅만 한다.
    """
    if not isinstance(result, dict):
        return
    defects = result.get("defects") or []
    if not defects:
        return
    grade = int(result.get("grade") or 0)
    # ★ 2026-08-26: 몇 등급부터 이벤트로 올릴지는 관리자가 S-95 화면에서
    #   조정할 수 있게 뺐다(core.settings.road_event_min_grade, 기본값 1 —
    #   지금까지의 동작 그대로: 손상이 하나라도 잡히면 등급 무관 전부 올림).
    from ..core import settings as ug_settings
    if grade < ug_settings.road_event_min_grade():
        return
    # 노면 등급 1~4 를 이벤트 등급으로 옮긴다. 등급 산정 기준은 현장
    # 캘리브레이션 후 조정해야 한다(docs/road_surface_management_plan.md).
    level = {1: "관심", 2: "관심", 3: "주의", 4: "경계"}.get(grade, "관심")
    kinds = sorted({(d.get("type") or "") for d in defects} - {""})
    db = None
    try:
        db = get_session()
        ev = events.record_detection(
            db, domain=Domain.ROAD.value,
            block_id=result.get("target") or "ROAD",
            place_name=result.get("target_name") or result.get("target") or "노면 감시구간",
            level=level, event_type="도로 노면 관리",
            detail={"손상 유형": ", ".join(kinds) or "미상",
                    "탐지 건수": len(defects),
                    "노면 등급": result.get("grade_label") or grade,
                    "분석 프레임": result.get("frames_analyzed")})
        if ev is not None:
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("노면 이벤트 기록 실패")
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if db is not None:
            db.close()


class EventSync(threading.Thread):
    """침수·교통 도메인 동기화. 이쪽은 PipelineRunner 가 상시 돌아 상태만
    읽으면 된다(2026-08-21 도메인 분리 당시 교통까지 이 스레드가 맡게 됐는데
    docstring 갱신을 놓쳤다 — 2026-08-22 정정, run() 은 처음부터
    _sync_traffic()+_sync_flood() 를 둘 다 부르고 있었다).

    ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — ``sync_fns``를 추가했다.
    platform-shell(옛 PipelineRunner, 롤백 경로)은 지금처럼 두 함수를
    다 부르지만, flood-service/traffic-service는 **자기 store에 자기
    도메인 필드만 있다**(예: flood 스냅샷엔 `traffic_enabled` 키 자체가
    없다). 예전처럼 두 함수를 무조건 다 부르면 `_sync_traffic()`의
    `snap.get("traffic_enabled", True)` 게이트가 **키가 없어서** 기본값
    True로 걸러지지 않고, flood 전용 스냅샷으로 가짜 "교통위험" 이벤트를
    만들 뻔했다(실기 검증 전 코드 검토로 발견) — 각 서비스가 자기
    도메인의 함수만 넘기게 했다."""

    def __init__(self, store, interval: float = INTERVAL_SEC, sync_fns=None):
        super().__init__(daemon=True, name="urbanguard-event-sync")
        self._store = store
        self._interval = interval
        self._stop_ev = threading.Event()
        self._sync_fns = sync_fns if sync_fns is not None else (_sync_traffic, _sync_flood)

    def stop(self) -> None:
        self._stop_ev.set()

    def run(self) -> None:
        # 기동 직후에는 탐지 결과가 없으므로 한 주기 쉬고 시작한다.
        while not self._stop_ev.wait(self._interval):
            db = None
            try:
                db = get_session()
                # ★ 2026-08-21: 한 지점에서 침수·교통 이벤트가 **각각**
                #   생길 수 있다(확정사항 ②). `open_event_for` 가
                #   (도메인, 지점) 조합으로 찾으므로 서로 섞이지 않는다.
                n = sum(fn(db, self._store) for fn in self._sync_fns)
                if n:
                    db.commit()
                else:
                    db.rollback()
            except Exception:  # noqa: BLE001
                # DB가 잠시 끊겨도 탐지·관제 화면은 계속 돌아야 한다.
                log.exception("이벤트 동기화 실패 — 다음 주기에 다시 시도합니다")
                if db is not None:
                    try:
                        db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                if db is not None:
                    db.close()
