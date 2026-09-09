"""외부 CCTV 목록 수집 — 국가교통정보센터(ITS) OpenAPI.

부산 교통 CCTV는 공개 API로 목록과 **HLS 스트림 주소**를 함께 받을 수 있다.
지점을 손으로 하나씩 넣던 것을 없애기 위한 경로다.

⚠️ **방범 CCTV는 여기서 받을 수 없다.** 방범 영상은 지자체 CCTV통합관제센터
전용망으로만 흐르고 외부 제공 창구가 없다(「개인정보 보호법」 제25조 관련).
이 모듈이 가져오는 것은 **교통 소통정보용 CCTV**뿐이다.

API 규격 (openapi.its.go.kr)
    GET https://openapi.its.go.kr:9443/cctvInfo
        apiKey  발급 키
        type    ex(고속도로) | its(국도·지자체)
        cctvType 1(실시간 스트리밍) | 2(동영상 파일) | 3(정지영상)
        minX/maxX/minY/maxY  경위도 범위
        getType json
    응답 response.data[] : cctvname, cctvurl, coordx, coordy, cctvformat …
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("urbanguard.cctv_sources")

API_URL = "https://openapi.its.go.kr:9443/cctvInfo"
TIMEOUT_SEC = 20

# 부산 광역 경계에 여유를 둔 사각형. 좌표 검증(33~39 / 124~132)과 어긋나지
# 않도록 국내 범위 안에 둔다.
BUSAN_BBOX = {"minX": 128.7, "maxX": 129.4, "minY": 34.9, "maxY": 35.45}

# 지역 프리셋 — 17개 시도. ITS OpenAPI 는 전국을 담당하므로 경위도 사각형만
# 바꾸면 어느 지역이든 같은 방식으로 받는다.
#
# ⚠️ **행정경계가 아니라 여유를 둔 사각형이다.** 이웃 시도가 섞여 들어오고
# 해안 지역은 바다도 포함한다. 지점을 고를 때 이름과 좌표를 함께 확인해야
# 한다. 자동 등록의 후보를 좁히는 용도이지 경계 판정용이 아니다.
REGIONS = {
    # 부울경 — 주 사업권역
    "busan": ("부산광역시", BUSAN_BBOX),
    "ulsan": ("울산광역시", {"minX": 128.9, "maxX": 129.5, "minY": 35.4, "maxY": 35.8}),
    "gyeongnam": ("경상남도", {"minX": 127.5, "maxX": 129.3, "minY": 34.5, "maxY": 35.9}),
    # 수도권
    "seoul": ("서울특별시", {"minX": 126.76, "maxX": 127.18, "minY": 37.42, "maxY": 37.70}),
    "incheon": ("인천광역시", {"minX": 126.37, "maxX": 126.80, "minY": 37.30, "maxY": 37.65}),
    "gyeonggi": ("경기도", {"minX": 126.30, "maxX": 127.85, "minY": 36.90, "maxY": 38.30}),
    # 충청·강원
    "gangwon": ("강원특별자치도", {"minX": 127.05, "maxX": 129.37, "minY": 37.00, "maxY": 38.62}),
    "chungbuk": ("충청북도", {"minX": 127.25, "maxX": 128.65, "minY": 36.00, "maxY": 37.25}),
    "chungnam": ("충청남도", {"minX": 125.95, "maxX": 127.60, "minY": 35.98, "maxY": 37.10}),
    "daejeon": ("대전광역시", {"minX": 127.25, "maxX": 127.56, "minY": 36.18, "maxY": 36.50}),
    "sejong": ("세종특별자치시", {"minX": 127.15, "maxX": 127.40, "minY": 36.42, "maxY": 36.72}),
    # 호남
    "jeonbuk": ("전북특별자치도", {"minX": 126.40, "maxX": 127.95, "minY": 35.35, "maxY": 36.15}),
    "jeonnam": ("전라남도", {"minX": 125.95, "maxX": 127.90, "minY": 33.90, "maxY": 35.50}),
    "gwangju": ("광주광역시", {"minX": 126.65, "maxX": 127.02, "minY": 35.05, "maxY": 35.26}),
    # 대경
    "gyeongbuk": ("경상북도", {"minX": 127.80, "maxX": 129.60, "minY": 35.65, "maxY": 37.55}),
    "daegu": ("대구광역시", {"minX": 128.35, "maxX": 128.78, "minY": 35.65, "maxY": 36.02}),
    # 제주
    "jeju": ("제주특별자치도", {"minX": 126.10, "maxX": 126.99, "minY": 33.10, "maxY": 33.60}),
}


class SourceError(RuntimeError):
    """수집 실패. 화면에 그대로 보여 줄 수 있는 문장을 담는다."""


def api_key() -> str:
    """ITS OpenAPI 키. `.env` 의 ITS_API_KEY 를 쓰고 BUSAN_API_KEY 도 받아준다."""
    return (os.environ.get("ITS_API_KEY")
            or os.environ.get("BUSAN_API_KEY") or "").strip()


def _request(params: dict) -> dict:
    url = API_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # noqa: PERF203
        raise SourceError(
            f"교통정보 API가 오류를 돌려줬습니다(HTTP {e.code}). "
            "키가 올바른지, 사용 승인이 났는지 확인하세요.") from e
    except Exception as e:  # noqa: BLE001
        raise SourceError(
            "교통정보 API에 접속하지 못했습니다. 관제망에서 외부 접속이 "
            f"막혀 있을 수 있습니다 — {str(e)[:80]}") from e


def _rows(payload: dict) -> list[dict]:
    """응답에서 데이터 배열만 꺼낸다. 래핑이 버전마다 달라 방어적으로 본다."""
    if not isinstance(payload, dict):
        return []
    resp = payload.get("response")
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _area(bbox: dict) -> float:
    return (bbox["maxX"] - bbox["minX"]) * (bbox["maxY"] - bbox["minY"])


def region_of(lat: float | None, lng: float | None) -> str | None:
    """좌표가 어느 시도에 드는지. 판정 못 하면 None.

    프리셋 사각형은 서로 겹친다(서울은 경기 안에, 부산은 경남 안에 들어간다).
    그래서 **드는 것 중 가장 좁은 사각형**을 고른다 — 좁을수록 구체적이다.

    별도 컬럼을 두지 않고 좌표에서 매번 판정한다. 지역은 좌표에서 나오는
    사실이라, 따로 저장하면 좌표를 고쳤을 때 어긋난다.
    """
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    hits = [(key, _area(b)) for key, (_label, b) in REGIONS.items()
            if b["minY"] <= lat_f <= b["maxY"] and b["minX"] <= lng_f <= b["maxX"]]
    if not hits:
        return None
    return min(hits, key=lambda x: x[1])[0]


def region_label(key: str | None) -> str:
    """지역 키를 사람이 읽는 이름으로. 모르는 지역은 「지역 미상」."""
    if key and key in REGIONS:
        return REGIONS[key][0]
    return "지역 미상"


def parse_resolution(text: str) -> tuple[int, int] | None:
    """``cctvresolution`` 문자열에서 가로·세로를 뽑는다. 못 읽으면 None.

    표기가 기관마다 달라(``1920x1080`` · ``1280*720`` · ``HD``) 넓게 받는다.
    **읽지 못한 것을 0 으로 두지 않는다** — 0 이면 「저해상도」로 잘못 걸러져
    쓸 만한 지점이 조용히 사라진다. 모르는 것은 모른다고 둔다.
    """
    s = (text or "").strip().lower().replace(" ", "")
    if not s:
        return None
    named = {"fhd": (1920, 1080), "fullhd": (1920, 1080), "1080p": (1920, 1080),
             "hd": (1280, 720), "720p": (1280, 720), "uhd": (3840, 2160),
             "4k": (3840, 2160), "sd": (720, 480), "480p": (640, 480)}
    if s in named:
        return named[s]
    for sep in ("x", "*", "×"):
        if sep in s:
            a, _, b = s.partition(sep)
            try:
                w, h = int(a), int(b)
            except ValueError:
                return None
            return (w, h) if w > 0 and h > 0 else None
    return None


def _norm(row: dict) -> dict | None:
    """API 한 줄을 등록용 형태로. 쓸 수 없는 줄은 None."""
    name = str(row.get("cctvname") or "").strip()
    url = str(row.get("cctvurl") or "").strip()
    if not name or not url.startswith(("http://", "https://")):
        return None
    try:
        lng = float(row.get("coordx"))
        lat = float(row.get("coordy"))
    except (TypeError, ValueError):
        return None
    res_text = str(row.get("cctvresolution") or "").strip()
    wh = parse_resolution(res_text)
    return {
        "name": name,
        "lat": lat,
        "lng": lng,
        "source_type": "hls",
        "source_url": url,
        "cctv_name": name,
        "resolution": res_text,
        "width": wh[0] if wh else None,
        "height": wh[1] if wh else None,
        "format": str(row.get("cctvformat") or "").strip(),
    }


def fetch(region: str = "busan", *, key: str | None = None,
          road_types: tuple[str, ...] = ("its", "ex"),
          min_height: int | None = None) -> list[dict]:
    """지역 CCTV 목록을 가져온다. **실시간 스트리밍(cctvType=1)만** 취한다.

    동영상 파일·정지영상은 관제에 쓸 수 없어 거른다.
    ``its``(국도·지자체)와 ``ex``(고속도로)를 모두 조회해 합친다.

    ``min_height`` 를 주면 그보다 낮은 지점을 뺀다(720 이면 HD 이상만).
    **해상도를 못 읽은 지점은 남긴다** — API 표기가 비어 있는 경우가 있어
    걸러 버리면 실제로는 고해상도인 지점까지 사라진다. 실측은
    ``scripts/probe_cctv_resolution.py`` 로 한다.
    """
    key = (key or api_key()).strip()
    if not key:
        raise SourceError(
            "교통정보 API 키가 없습니다. openapi.its.go.kr 에서 발급받아 "
            ".env 의 ITS_API_KEY 에 넣고 서비스를 재시작하세요.")
    if region not in REGIONS:
        raise SourceError(f"알 수 없는 지역입니다: {region}")

    _, bbox = REGIONS[region]
    out: list[dict] = []
    seen: set[str] = set()
    for rtype in road_types:
        params = {"apiKey": key, "type": rtype, "cctvType": 1,
                  "getType": "json", **bbox}
        for raw in _rows(_request(params)):
            item = _norm(raw)
            if item is None:
                continue
            # 같은 지점이 두 유형에 중복으로 잡히는 경우가 있다.
            if item["source_url"] in seen:
                continue
            seen.add(item["source_url"])
            item["road_type"] = rtype
            out.append(item)

    if min_height is not None:
        kept = [x for x in out
                if x["height"] is None or x["height"] >= min_height]
        log.info("해상도 %dp 미만 제외 %d개소", min_height, len(out) - len(kept))
        out = kept

    # 해상도 높은 순 → 이름 순. 표기가 없는 지점은 뒤로 보낸다.
    out.sort(key=lambda x: (-(x["height"] or 0), x["name"]))
    log.info("교통 CCTV 수집 region=%s %d개소", region, len(out))
    return out
