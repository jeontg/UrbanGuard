"""멀티뷰 화면 배치와 타일 프레임 (S-05).

**왜 있는가.** 경남 제안요청서 SFR-001 이 「관제 요원의 편의에 따라 **화면
분할, 이벤트 목록창 배치 등 레이아웃을 자유롭게 구성하고 저장**」을,
SFR-003 이 「이벤트 발생 시 해당 채널을 **시각적으로 강조**」를 요구한다.

지금 우리 상황판(S-01)은 **지점 카드 목록**이지 영상 다분할 화면이 아니다.
관제요원이 하루 종일 보는 화면이 없다는 뜻이라, 세 RFP 대비 가장 큰 구멍이다.

⚠️ **실시간 영상(HLS)이 아니라 서버 스냅샷을 쓴다.** 두 가지 이유다.

1. **망분리** — 기존 `index.html` 이 hls.js 를 CDN 에서 불러오는데, 이는
   폐쇄망 납품에서 그대로 깨진다. 여기서 같은 빚을 지지 않는다
2. **비식별** — :mod:`.snapshot` 은 사람 영역 마스킹을 적용한다. 다분할
   화면은 여러 명이 동시에 보는 자리라 마스킹이 더 중요하다

실시간 영상 전환은 hls.js 로컬 반입과 동시 디코딩 부하 검증이 선행돼야 한다.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Camera, MultiviewLayout  # noqa: F401

log = logging.getLogger("urbanguard.multiview")

# 허용 분할 수. 통상 사양은 64~128채널이지만(202608172039 조사),
# 스냅샷 방식에서 그만큼 띄우면 서버가 감당하지 못한다. **실측 없이 늘리지
# 않는다** — 숫자를 키우는 것은 쉽고 되돌리는 것은 어렵다.
TILE_CHOICES = (4, 9, 16)
DEFAULT_TILES = 4

DEFAULT_NAME = "기본"

# 스냅샷 캐시 수명(초). 16칸이 각자 원본을 다시 잡으면 서버가 멈춘다.
# 관제 화면 갱신 주기(5초)보다 짧게 두어 「같은 그림이 굳어 보이는」 일은 없게 한다.
CACHE_TTL_SEC = 4.0

_cache: dict[str, tuple[float, str, str]] = {}   # cam_id -> (시각, b64, mime)
_lock = threading.Lock()


# --- 배치 -------------------------------------------------------------------


def layouts_of(db: Session, user_id: int) -> list[MultiviewLayout]:
    return list(db.scalars(
        select(MultiviewLayout)
        .where(MultiviewLayout.user_id == user_id)
        .order_by(MultiviewLayout.is_default.desc(), MultiviewLayout.name)))


def default_layout(db: Session, user_id: int) -> MultiviewLayout | None:
    rows = layouts_of(db, user_id)
    return rows[0] if rows else None


def normalize(cameras: list[str], tiles: int) -> list[str]:
    """칸 수에 맞춰 목록 길이를 맞춘다.

    ⚠️ **빈 칸은 빈 문자열로 자리를 지킨다.** 목록을 당겨 버리면 사용자가
    정한 배치가 무너진다 — 3번 칸을 비웠는데 4번이 3번으로 올라오면
    「내가 놓은 자리」가 아니게 된다.
    """
    got = [(c or "").strip() for c in (cameras or [])][:tiles]
    return got + [""] * (tiles - len(got))


def save(db: Session, *, user_id: int, name: str, tiles: int,
         cameras: list[str], make_default: bool = False
         ) -> tuple[MultiviewLayout | None, list[str]]:
    name = (name or "").strip() or DEFAULT_NAME
    if tiles not in TILE_CHOICES:
        return None, [f"분할 수는 {', '.join(map(str, TILE_CHOICES))} 중에서 "
                      "고를 수 있습니다."]
    if len(name) > 64:
        return None, ["배치 이름이 너무 깁니다."]

    row = db.scalars(
        select(MultiviewLayout)
        .where(MultiviewLayout.user_id == user_id,
               MultiviewLayout.name == name)).first()
    if row is None:
        row = MultiviewLayout(user_id=user_id, name=name)
        db.add(row)
    row.tiles = tiles
    row.cameras = normalize(cameras, tiles)
    if make_default:
        for other in layouts_of(db, user_id):
            if other is not row:
                other.is_default = False
        row.is_default = True
    db.flush()
    return row, []


def delete(db: Session, *, user_id: int, layout_id: int) -> bool:
    row = db.get(MultiviewLayout, layout_id)
    # ⚠️ 남의 배치를 지울 수 없다. id 만 보고 지우면 URL 을 바꿔 남의 것을
    # 지울 수 있다.
    if row is None or row.user_id != user_id:
        return False
    db.delete(row)
    db.flush()
    return True


# --- 타일 프레임 ------------------------------------------------------------


def frame_of(db: Session, camera_id: str) -> tuple[str, str, str]:
    """타일에 띄울 한 장. ``(base64, 마스킹 상태, 오류문구)``.

    캐시가 살아 있으면 그대로 준다. 없으면 원본에서 한 장 잡는다.

    ⚠️ **실패를 예외로 올리지 않는다.** 한 칸이 안 나온다고 다분할 화면 전체가
    깨지면 안 된다 — 나머지 15칸은 여전히 관제해야 한다.

    ⚠️ **마스킹 상태를 그대로 올려 보낸다.** :func:`~.snapshot.grab` 은
    마스킹에 실패해도 영상을 내보내므로, **가리지 못했다는 사실이 화면까지
    가야** 한다. 여러 명이 동시에 보는 다분할 화면에서는 더 중요하다.
    """
    now = time.time()
    with _lock:
        got = _cache.get(camera_id)
        if got and now - got[0] < CACHE_TTL_SEC:
            return got[1], got[2], ""

    cam = db.get(Camera, camera_id)
    if cam is None:
        return "", "", "지점을 찾을 수 없습니다."
    try:
        from . import snapshot as SNAP
        b64, mask_state, err = SNAP.grab(cam, mask=True)
    except Exception as e:  # noqa: BLE001
        log.warning("타일 프레임 실패 %s: %s", camera_id, str(e)[:120])
        return "", "", "영상을 가져오지 못했습니다."
    if not b64:
        return "", "", err or "영상을 가져오지 못했습니다."
    with _lock:
        _cache[camera_id] = (now, b64, mask_state)
    return b64, mask_state, ""


def clear_cache() -> None:
    with _lock:
        _cache.clear()
