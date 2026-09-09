"""노면 탐지 결과 보관 (지점별 최신 1건).

노면 현황 화면(S-40)이 지금까지 **모의 데이터**를 보여 준 이유는, 분석 결과를
어디에도 남기지 않았기 때문이다. 상시 순회도 선택 실행도 결과를 화면에 한 번
띄우고 버렸다.

여기에 모아 두면 두 경로가 같은 저장소를 채우고, 화면은 **실제로 무엇이
탐지됐는지**를 보여 줄 수 있다. 분석하지 않은 지점은 「미분석」으로 남는다 —
모의값으로 채우면 운영자가 실제 점검 결과로 오해한다.

⚠️ 메모리 저장이라 **재시작하면 사라진다.** 이력 보존이 필요하면 이벤트
(`events` 테이블)를 봐야 한다 — 손상이 잡히면 그쪽에도 기록된다.

실시간 관제(S-44)를 붙이면서 두 가지를 더 남긴다.

* **경과 시간(``age_sec``)** — 상시 순회는 지점당 15분 주기라, 화면에 뜬 등급이
  방금 것인지 15분 전 것인지 구분되지 않으면 결빙·낙하물 대응을 그르친다.
* **최근 이력(``history``)** — 집중 감시로 같은 지점을 반복 관측할 때 손상
  건수가 늘고 있는지 봐야 한다. 최신 1건만으로는 추세를 알 수 없다.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger("urbanguard.road_results")

# 지점당 보관할 최근 결과 수. 집중 감시(60초 주기)로 20분치에 해당한다.
# 메모리 저장이라 무한정 쌓으면 장기 운영에서 새어 나간다.
HISTORY_LIMIT = 20

_lock = threading.Lock()


# 지점별 구간 길이 캐시. {camera_id: (설정판번호, 길이 또는 None)}
#
# ⚠️ 캐시가 없으면 **관측할 때마다 DB를 친다.** 기록은 상시 탐지 루프의
# 핫패스라, DB가 느리거나 죽어 있으면 그 지연이 순회 전체를 끌어내린다
# (실제로 시험이 3배 느려져 발견했다). 구간 길이는 거의 바뀌지 않는 값이라
# 캐시하는 것이 맞다.
#
# 무효화는 **설정 판번호**로 한다 — S-80 에서 보정을 저장하면 판번호가 오르고,
# 그 순간 캐시가 저절로 무효가 된다. 시간 만료를 쓰면 「저장했는데 한동안
# 옛값이 나오는」 상태가 생긴다.
_section_cache: dict[str, tuple[int, float | None]] = {}


def _section_length(camera_id: str) -> float | None:
    """이 지점의 담당 구간 길이(m). 보정 전이면 None. 결과를 캐시한다."""
    try:
        from ..core import config_rev
        rev = config_rev.revision()
    except Exception:  # noqa: BLE001
        rev = 0

    cached = _section_cache.get(camera_id)
    if cached is not None and cached[0] == rev:
        return cached[1]

    length: float | None = None
    try:
        from ..core import calibration
        from ..core import cameras as _cams
        from ..core.db import get_session
        db = get_session()
        try:
            cam = _cams.get(db, camera_id)
            if cam is not None:
                cal = calibration.of(cam, "road")
                length = cal.section.length_m if cal.section else None
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        # DB를 못 읽어도 관제가 멈추면 안 된다. 미보정으로 내려가되,
        # **실패도 캐시한다** — 안 그러면 DB가 죽어 있는 동안 관측마다
        # 연결을 재시도하며 순회가 느려진다.
        log.debug("구간 보정 조회 실패 camera_id=%s", camera_id, exc_info=True)

    _section_cache[camera_id] = (rev, length)
    return length



def section_length(camera_id: str) -> float | None:
    """이 지점의 구간 길이(m). 보정 안 됐으면 ``None``.

    ★ **왜 공개하는가 (2026-08-19 전수조사)**

    실측 결과 **39지점 전부 구간 길이가 비어 있어**(0/39) 정비 등급이 늘
    「미보정」이었다. 입력 화면(S-80)은 있는데 **아무도 채우지 않았고, 화면
    어디에도 그 사실이 드러나지 않았다.**

    ⚠️ 「미보정」만 보이면 **고장으로 읽거나 그냥 넘긴다.** 몇 지점이 비어
    있는지, 어디서 채우는지를 화면이 말해야 한다.

    조회는 :func:`_section_length` 의 캐시를 그대로 쓴다 — 10초마다 부르는
    화면에서 매번 DB 를 때리면 안 된다.
    """
    return _section_length(camera_id)


def _density_of(camera_id: str, defect_count: int):
    """(건/100m, 단계). 구간 길이를 보정하지 않았으면 (None, "미보정")."""
    from ..core import calibration

    length = _section_length(camera_id)
    if length is None:
        return None, "미보정"
    per = calibration.RoadSection(length_m=length).per_100m(defect_count)

    # 구간 표(S-95)를 읽어 판정한다. 못 읽으면 코드 기본값으로 내려간다 —
    # **DB 가 죽었다고 노면 판정이 멎으면 안 된다.**
    db = None
    try:
        from ..core.db import get_session
        db = get_session()
        level = calibration.road_level(per, db=db)
    except Exception:  # noqa: BLE001
        log.debug("노면 등급 구간 조회 실패 camera_id=%s", camera_id, exc_info=True)
        level = calibration.road_level(per)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    return (round(per, 2) if per is not None else None, level)
_results: dict[str, dict] = {}
_history: dict[str, list[dict]] = {}


def record(camera_id: str, result: dict, *, source: str = "") -> None:
    """분석 결과 한 건을 지점에 기록한다.

    ``source`` 는 어느 경로로 관측했는지다(``continuous``/``focus``/``manual``).
    이력 표에서 **상시 순회 결과와 사람이 눌러 돌린 결과를 구분**해야, 「왜 이때만
    값이 튀지」를 설명할 수 있다.
    """
    if not camera_id or not isinstance(result, dict):
        return
    item = dict(result)
    item["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    item["source"] = source or item.get("source") or ""
    frames = item.get("frames_analyzed", 0) or 0
    # 구간 길이를 보정해 둔 지점이면 「건/100m」로도 함께 남긴다.
    # 개수만으로는 **긴 구간이 항상 불리해** 지점끼리 비교가 되지 않는다.
    # 보정 전에는 None 이며, 화면은 그것을 「미보정」으로 표시한다.
    item["per_100m"], item["density_level"] = _density_of(
        camera_id, len(item.get("defects") or []))
    # 이력은 카드 한 장에 담을 요약만 남긴다. 결과 전체를 쌓으면 탐지
    # 박스까지 20벌씩 들고 있게 된다.
    entry = {
        "analyzed_at": item["analyzed_at"],
        "defect_count": len(item.get("defects") or []),
        "grade": item.get("grade"),
        "frames_analyzed": frames,
        "failed": frames <= 0,
        "source": item["source"],
        "per_100m": item["per_100m"],
    }
    with _lock:
        _results[camera_id] = item
        h = _history.setdefault(camera_id, [])
        h.append(entry)
        if len(h) > HISTORY_LIMIT:
            del h[:-HISTORY_LIMIT]
    _persist(camera_id, item, entry)


def get(camera_id: str) -> dict | None:
    with _lock:
        return _results.get(camera_id)


def _persist(camera_id: str, item: dict, entry: dict) -> None:
    """이력을 DB에도 남긴다(C단계). 실패해도 호출자를 막지 않는다.

    ⚠️ **관제 분석이 DB 때문에 멈추면 안 된다.** DB가 죽어 있어도 메모리
    이력과 화면은 그대로 동작해야 하므로, 여기서 나는 오류는 전부 삼킨다.
    """
    try:
        from ..core import road_history
        road_history.save(camera_id, item.get("target_name") or "", entry)
    except Exception:  # noqa: BLE001
        log.debug("점검 이력 DB 저장 실패 camera=%s", camera_id, exc_info=True)


def history_is_persistent() -> bool:
    """이력이 재시작 후에도 남는가. 화면이 그 사실을 알려야 한다."""
    try:
        from ..core import road_history
        return road_history.available()
    except Exception:  # noqa: BLE001
        return False


def history(camera_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """지점의 최근 관측 이력(오래된 것부터).

    **DB에 있으면 그쪽을 먼저 본다** — 재시작 후에도 이력이 이어져야 한다.
    DB를 읽지 못하면 메모리로 내려간다(서비스가 멈추는 것보다 낫다).
    """
    limit = max(limit, 1)
    try:
        from ..core import road_history
        rows = road_history.recent(camera_id, limit)
        if rows is not None:
            return rows
    except Exception:  # noqa: BLE001
        log.debug("점검 이력 DB 조회 실패 camera=%s", camera_id, exc_info=True)
    with _lock:
        h = _history.get(camera_id) or []
        return [dict(x) for x in h[-limit:]]


def all_results() -> dict[str, dict]:
    with _lock:
        return dict(_results)


def clear() -> None:
    with _lock:
        _results.clear()
        _history.clear()
        # 구간 보정 캐시도 함께 비운다. 시험이 카메라를 만들고 지우기를
        # 반복하는데, 캐시가 남아 있으면 지운 지점의 값이 되살아난다.
        _section_cache.clear()


def age_seconds(analyzed_at: str | None) -> float | None:
    """관측 시각으로부터 지난 시간(초). 파싱에 실패하면 ``None``.

    ``None`` 을 0으로 바꾸지 않는다 — 0초는 「방금 관측함」이라는 뜻이라,
    시각을 모르는 상태와 섞이면 오래된 결과가 최신으로 보인다.
    """
    if not analyzed_at:
        return None
    try:
        t = datetime.fromisoformat(analyzed_at)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - t).total_seconds(), 0.0)


def summary(camera_id: str, name: str) -> dict:
    """노면 현황 카드 한 장에 필요한 값.

    분석 이력이 없으면 등급을 매기지 않는다(`grade=None`). 0등급으로 두면
    화면에서 「정상」처럼 보인다.
    """
    r = get(camera_id)
    if r is None:
        return {"block_id": camera_id, "name": name, "analyzed": False,
                "failed": False, "grade": None, "grade_label": "미분석",
                "defect_count": 0, "analyzed_at": None, "note": "",
                "frames_analyzed": 0, "age_sec": None}

    frames = r.get("frames_analyzed", 0) or 0
    defects = r.get("defects") or []
    # ⚠️ 프레임을 한 장도 못 받았으면 **분석한 것이 아니다.**
    # 손상 0건 → 등급 「정상」이 그대로 나오면, 스트림이 끊겨 아무것도 못 본
    # 구간을 「점검했더니 이상 없음」으로 오해한다.
    if frames <= 0:
        return {"block_id": camera_id, "name": name, "analyzed": False,
                "failed": True, "grade": None, "grade_label": "분석 실패",
                "defect_count": 0, "analyzed_at": r.get("analyzed_at"),
                "note": r.get("note") or "영상을 받지 못했습니다.",
                "frames_analyzed": 0,
                "age_sec": age_seconds(r.get("analyzed_at"))}

    return {
        "block_id": camera_id, "name": name, "analyzed": True, "failed": False,
        "grade": r.get("grade"),
        "grade_label": r.get("grade_label") or "",
        "defect_count": len(defects),
        "analyzed_at": r.get("analyzed_at"),
        "frames_analyzed": frames,
        "note": r.get("note") or "",
        "age_sec": age_seconds(r.get("analyzed_at")),
    }
