"""서울 CCTV 목록 수집 — 서울시 교통정보시스템(TOPIS).

ITS OpenAPI(`cctv_sources`)와 달리 **인증키가 필요 없다.** 부산 지점을 손으로
넣어 쓰던 것과 같은 방식으로, 공개된 HLS 주소를 그대로 받아 등록한다.

한 지점이 주소를 둘 가진다.

* ``hlsUrl``  — 서울시 자체(`topiscctv1.eseoul.go.kr`)
* ``remark5`` — `strm1~4.spatic.go.kr`

실측(2026-08-16, 표본 30개소)에서 **spatic 쪽이 같거나 더 높았다.** 같은
카메라가 자체 주소로는 720x480, spatic 으로는 1280x720 이었다. 그래서
**spatic 을 우선 쓰고 없을 때만 자체 주소로 내려간다.**

⚠️ **공식 제공 창구가 아니다.** 웹 화면이 쓰는 경로라 예고 없이 바뀔 수 있고,
「고정적 다중 접속 시 자동 차단」 안내가 있다. 상시 탐지로 돌리기 전에 서울시
교통정보센터와 연계 협의가 필요하다(docs 202608160906 조사서 3절).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("urbanguard.topis_sources")

BASE = "https://topis.seoul.go.kr"
LIST_URL = f"{BASE}/map/cctv/selectCctvList.do"
INFO_URL = f"{BASE}/map/selectCctvInfo.do"

TIMEOUT_SEC = 15
PAGE_SIZE = 10          # 서버가 쪽당 10건으로 고정한다. 늘려 달라 해도 안 준다.
POLITE_DELAY_SEC = 0.3  # 남의 서버다. 몰아치지 않는다.

# 서울 좌표 범위. 응답이 엉뚱한 값을 주면 등록에서 걸러낸다.
SEOUL_BOUNDS = {"minX": 126.6, "maxX": 127.3, "minY": 37.3, "maxY": 37.8}


class SourceError(RuntimeError):
    """수집 실패. 화면에 그대로 보여 줄 수 있는 문장을 담는다."""


def _post(url: str, params: dict) -> dict | list:
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        # 사이트가 브라우저 요청만 받는 경우가 있어 최소한의 신원을 밝힌다.
        "User-Agent": "UrbanGuard/1.0 (+NCI)",
        "Referer": f"{BASE}/map/openCctvMap.do",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SourceError(
            f"서울 교통정보시스템이 오류를 돌려줬습니다(HTTP {e.code}). "
            "잠시 뒤 다시 시도하세요.") from e
    except Exception as e:  # noqa: BLE001
        raise SourceError(
            "서울 교통정보시스템에 접속하지 못했습니다. 관제망에서 외부 접속이 "
            f"막혀 있을 수 있습니다 — {str(e)[:80]}") from e


def _rows(payload) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("rows")
        return rows if isinstance(rows, list) else []
    return payload if isinstance(payload, list) else []


def stream_url(info: dict) -> str:
    """재생 주소 하나를 고른다. **spatic 우선** — 실측에서 화질이 더 높았다."""
    for key in ("remark5", "hlsUrl", "hlsUrlOri"):
        url = str(info.get(key) or "").strip()
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _norm(cam: dict, info: dict) -> dict | None:
    """목록 한 줄 + 상세 한 줄을 등록용 형태로. 쓸 수 없으면 None."""
    name = str(cam.get("camName") or "").strip()
    url = stream_url(info)
    if not name or not url:
        return None
    try:
        lat = float(cam.get("lat"))
        lng = float(cam.get("lng"))
    except (TypeError, ValueError):
        return None
    b = SEOUL_BOUNDS
    if not (b["minY"] <= lat <= b["maxY"] and b["minX"] <= lng <= b["maxX"]):
        # 좌표가 서울 밖이면 지도에 엉뚱하게 찍힌다. 등록하지 않는다.
        return None
    return {
        "name": name,
        "lat": lat,
        "lng": lng,
        "source_type": "hls",
        "source_url": url,
        "cctv_name": name,
        "cam_id": str(cam.get("camId") or "").strip(),
        # 해상도는 이 API 가 알려주지 않는다. **모른다고 둔다** —
        # scripts/probe_cctv_resolution.py 로 실측한다.
        "resolution": "",
        "width": None,
        "height": None,
    }


def fetch_list(max_count: int = 60) -> list[dict]:
    """지점 목록만 받는다(주소 없음). 쪽당 10건이라 여러 번 부른다."""
    out: list[dict] = []
    page = 1
    while len(out) < max_count:
        payload = _post(LIST_URL, {"pageIndex": page})
        rows = _rows(payload)
        if not rows:
            break
        for r in rows:
            out.append({"camId": str(r.get("camId") or "").strip(),
                        "camName": str(r.get("camName") or "").strip(),
                        "lat": r.get("lat"), "lng": r.get("lng")})
        total = 0
        if isinstance(payload, dict):
            total = int(payload.get("TotalRows") or 0)
        if total and len(out) >= total:
            break
        page += 1
        time.sleep(POLITE_DELAY_SEC)
    return out[:max_count]


def fetch_info(cam_id: str) -> dict:
    """지점 상세. 여기에 재생 주소가 들어 있다."""
    rows = _rows(_post(INFO_URL, {"camId": cam_id, "cctvSourceCd": "HP"}))
    return rows[0] if rows else {}


def fetch(max_count: int = 60, *, name_filter: str = "") -> list[dict]:
    """등록에 바로 쓸 수 있는 형태로 목록을 만든다.

    ``max_count`` 는 **상세 조회 횟수**를 묶는 안전장치다. 510개소를 한 번에
    긁으면 상대 서버에 부담이고 우리도 오래 기다린다.
    ``name_filter`` 를 주면 지점명에 그 글자가 든 것만 상세를 조회한다.
    """
    cams = fetch_list(max_count=600 if name_filter else max_count)
    if name_filter:
        key = name_filter.strip()
        cams = [c for c in cams if key in c["camName"]]
    cams = cams[:max_count]

    out: list[dict] = []
    seen: set[str] = set()
    for c in cams:
        if not c["camId"]:
            continue
        try:
            info = fetch_info(c["camId"])
        except SourceError:
            log.warning("TOPIS 상세 실패 camId=%s", c["camId"])
            continue
        item = _norm(c, info)
        time.sleep(POLITE_DELAY_SEC)
        if item is None or item["source_url"] in seen:
            continue
        seen.add(item["source_url"])
        out.append(item)

    out.sort(key=lambda x: x["name"])
    log.info("서울 TOPIS 수집 %d개소", len(out))
    return out
