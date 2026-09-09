"""통합 상황판 지도 — 좌표를 화면 위치로 바꾼다 (S-01 ②영역).

외부 지도 타일을 쓰지 않는다. 지자체 관제망은 폐쇄망인 경우가 많아 외부
서비스가 아예 뜨지 않기 때문이다(설계서 9-4절). 블록 위경도를 뷰포트에
정규화해 자체 SVG로 그린다.

정확한 배경지도가 필요하면 지자체 GIS 서버 연계가 1순위이고, 그전까지는
이 「단순 도식」이 최소 대안이다. 배경 이미지를 설정으로 받을 수 있도록
좌표 정규화 규칙(경계 상자 + 여백)을 고정해 둔다.
"""
from __future__ import annotations

from ..core import events as E
from ..core import roles as R

PAD = 9.0          # 가장자리 여백(%) — 마커가 잘리지 않도록
MIN_SPAN = 0.004   # 블록이 몰려 있을 때 최소 경계 상자 크기(도)

LEVEL_COLOR = {"심각": "#e5484d", "경계": "#e8843b",
               "주의": "#d4a017", "관심": "#3fb950"}
NORMAL_COLOR = "#3fb950"


def _span(values: list[float]) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    if hi - lo < MIN_SPAN:
        mid = (lo + hi) / 2
        lo, hi = mid - MIN_SPAN / 2, mid + MIN_SPAN / 2
    return lo, hi


def build_points(blocks: list[dict], db=None, *,
                 allowed_domains: set[str] | None = None) -> list[dict]:
    """블록 목록과 현재 이벤트를 합쳐 지도에 찍을 점을 만든다.

    이벤트가 없는 지점도 「정상」으로 표시한다. 빈 자리가 곧 정보이기 때문이다 —
    지점이 지도에서 사라지면 관제요원은 고장인지 정상인지 알 수 없다.
    """
    coords = [(b.get("coordinates") or {}) for b in blocks]
    lats = [c.get("lat") for c in coords if c.get("lat") is not None]
    lngs = [c.get("lng") for c in coords if c.get("lng") is not None]
    if not lats or not lngs:
        return []
    lat_lo, lat_hi = _span(lats)
    lng_lo, lng_hi = _span(lngs)

    # 블록별 활성 이벤트 (가장 등급이 높은 것 하나)
    by_block: dict[str, object] = {}
    if db is not None:
        try:
            for ev in E.list_events(db, status="active",
                                    allowed_domains=allowed_domains, limit=500):
                cur = by_block.get(ev.block_id)
                if cur is None or E.rank(ev.level) > E.rank(cur.level):
                    by_block[ev.block_id] = ev
        except Exception:  # noqa: BLE001
            by_block = {}

    points = []
    for b in blocks:
        c = b.get("coordinates") or {}
        lat, lng = c.get("lat"), c.get("lng")
        if lat is None or lng is None:
            continue
        # 위도는 위쪽이 북쪽이 되도록 뒤집는다.
        x = PAD + (lng - lng_lo) / (lng_hi - lng_lo) * (100 - PAD * 2)
        y = PAD + (lat_hi - lat) / (lat_hi - lat_lo) * (100 - PAD * 2)
        ev = by_block.get(b.get("id"))
        points.append({
            "block_id": b.get("id"),
            "name": b.get("name") or b.get("id"),
            "x": round(x, 2), "y": round(y, 2),
            # 배치 도식은 백분율로 찍지만 **배경지도는 실제 좌표가 필요하다.**
            # 둘을 함께 넘겨 화면이 어느 쪽이든 그릴 수 있게 한다.
            "lat": lat, "lng": lng,
            "level": getattr(ev, "level", "") if ev else "",
            "color": LEVEL_COLOR.get(getattr(ev, "level", ""), NORMAL_COLOR),
            "domain": getattr(ev, "domain", "") if ev else "",
            "domain_label": (R.DOMAIN_LABELS.get(R.Domain(ev.domain), ev.domain)
                             if ev and ev.domain in {d.value for d in R.Domain}
                             else ""),
            "event_id": getattr(ev, "id", None) if ev else None,
            "status": (E.STATUS_LABELS.get(ev.status, ev.status) if ev else "정상"),
            "alert": bool(ev),
        })
    return points
