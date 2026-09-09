"""기상 관측·예보 수집 — 기상청 단기예보 조회서비스.

왜 필요한가
    노면 열화 예측의 입력 변수가 **기상에 몰려 있다.** 국내 예측모형
    (김도완 외, 2019, 한국방재학회)의 입력 8종 중 5종이 기상이다 —
    기온·수증기압·적설·초상온도·강수량.

    그리고 **침수 도메인도 강수량이 필요하다.** 한 번 붙이면 두 도메인이
    함께 이득이라 우선순위를 올렸다.

무엇을 가져오는가
    초단기실황(`getUltraSrtNcst`) — 기온(T1H)·습도(REH)·1시간 강수량(RN1)·
    강수형태(PTY)·풍속(WSD). 관측값이라 「지금 어땠는가」를 남기는 데 맞다.

    ⚠️ **적설·초상온도는 이 오퍼레이션에 없다.** 논문의 입력 5종을 전부
    채우려면 기상자료개방포털의 종관/방재기상관측(ASOS/AWS)이 따로 필요하다.
    지금 채울 수 있는 것만 채우고, 없는 것은 없다고 남긴다.

키가 없으면
    **서비스는 그대로 돈다.** 수집만 건너뛴다. 관제 화면이 기상 때문에
    멈추는 일은 없어야 한다.

규격 (apis.data.go.kr / 1360000)
    GET .../VilageFcstInfoService_2.0/getUltraSrtNcst
        serviceKey, pageNo, numOfRows, dataType=JSON,
        base_date=YYYYMMDD, base_time=HHmm, nx, ny
    응답 response.header.resultCode == "00" 이어야 정상
    ⚠️ **HTTP 200 이어도 resultCode 로 성공 여부를 다시 봐야 한다.**
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger("urbanguard.weather")

SERVICE = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
BASE = f"{SERVICE}/getUltraSrtNcst"          # 초단기실황 — 지금 관측
FCST_SHORT = f"{SERVICE}/getUltraSrtFcst"    # 초단기예보 — 6시간
FCST_VILLAGE = f"{SERVICE}/getVilageFcst"    # 단기예보 — 3일
TIMEOUT_SEC = 15

# 관측·예보 항목. 코드는 기상청 규격이며 화면에는 한글로 보여 준다.
# **모르는 코드는 버리지 않고 코드 그대로 보여 준다** — 규격이 늘었을 때
# 조용히 사라지면 「원래 없는 값」과 구분되지 않는다.
CATEGORY_LABELS = {
    # 초단기실황·초단기예보
    "T1H": ("기온", "℃"),
    "RN1": ("1시간 강수량", "mm"),
    "REH": ("습도", "%"),
    "PTY": ("강수형태", ""),
    "WSD": ("풍속", "m/s"),
    "SKY": ("하늘상태", ""),
    "VEC": ("풍향", "°"),
    "LGT": ("낙뢰", "kA"),
    # 단기예보
    "TMP": ("기온", "℃"),
    "TMX": ("일 최고기온", "℃"),
    "TMN": ("일 최저기온", "℃"),
    "POP": ("강수확률", "%"),
    "PCP": ("1시간 강수량", "mm"),
    "SNO": ("1시간 신적설", "cm"),
    "WAV": ("파고", "m"),
}

# 하늘상태 코드. 숫자로 두면 화면에서 읽을 수 없다.
SKY_LABELS = {"1": "맑음", "3": "구름많음", "4": "흐림"}

# 관제 요약줄에서 뺄 항목. **값을 버리는 것이 아니라 요약에서만 감춘다** —
# 필요해지면 그대로 꺼내 쓸 수 있어야 한다.
#
#  UUU·VVV — 바람의 동서·남북 성분. 풍속(WSD)·풍향(VEC)이 이미 있어 겹친다
#  WAV     — 파고. 우리 세 도메인(도로 침수·인파·노면) 어디에도 쓰지 않는다
SUMMARY_SKIP = ("UUU", "VVV", "WAV")

# 도메인별로 **실제 판단에 쓰는 항목**만 추린다. 전부 늘어놓으면 관제요원이
# 무엇을 봐야 하는지 알 수 없다.
#
#  침수 — 지금 오는 비와 앞으로 올 비
#  인파 — 기온(폭염·한파)과 강풍, 비 여부
#  노면 — 동결융해(최고·최저기온)와 적설, 강수
DOMAIN_CATEGORIES = {
    "flood": ("RN1", "PCP", "POP", "PTY"),
    "crowd": ("T1H", "TMP", "TMX", "TMN", "WSD", "PTY", "SKY"),
    "road": ("T1H", "TMP", "TMX", "TMN", "SNO", "RN1", "PCP", "REH"),
}

# 도메인별로 「왜 이 값을 보는가」. 화면에 함께 적어야 숫자가 뜻을 갖는다.
DOMAIN_WHY = {
    "flood": "지금 오는 비와 앞으로 올 비가 침수 위험의 선행 지표입니다.",
    "crowd": "폭염·한파와 강풍은 야외 인파 위험을 키웁니다.",
    "road": "동결융해 반복과 제설이 포장 열화의 주요 원인입니다.",
}

# 강수형태 코드. 0 이 「없음」이라 그대로 두면 결측과 헷갈린다.
PTY_LABELS = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈",
              "5": "빗방울", "6": "빗방울눈날림", "7": "눈날림"}

# --- 기상청 격자 변환 상수 (Lambert Conformal Conic) ---------------------
# 기상청 「동네예보 격자 정보」 규격. 전국을 5km 격자 149 x 253 으로 나눈다.
_RE = 6371.00877     # 지구 반경(km)
_GRID = 5.0          # 격자 간격(km)
_SLAT1, _SLAT2 = 30.0, 60.0     # 표준 위도
_OLON, _OLAT = 126.0, 38.0      # 기준점 경위도
_XO, _YO = 43, 136              # 기준점 격자 좌표


class SourceError(RuntimeError):
    """수집 실패. 화면에 그대로 보여 줄 수 있는 문장을 담는다."""


def api_key() -> str:
    """공공데이터포털 인증키. `.env` 의 KMA_API_KEY."""
    return (os.environ.get("KMA_API_KEY")
            or os.environ.get("WEATHER_API_KEY") or "").strip()


def to_grid(lat: float, lng: float) -> tuple[int, int]:
    """위경도 → 기상청 격자(nx, ny).

    기상청이 공개한 변환식 그대로다. 좌표를 그냥 넘기면 API 가 받지 않는다.
    """
    deg = math.pi / 180.0
    sn = (math.tan(math.pi * 0.25 + _SLAT2 * deg * 0.5)
          / math.tan(math.pi * 0.25 + _SLAT1 * deg * 0.5))
    sn = math.log(math.cos(_SLAT1 * deg) / math.cos(_SLAT2 * deg)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + _SLAT1 * deg * 0.5)
    sf = math.pow(sf, sn) * math.cos(_SLAT1 * deg) / sn
    ro = math.tan(math.pi * 0.25 + _OLAT * deg * 0.5)
    ro = _RE / _GRID * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + lat * deg * 0.5)
    ra = _RE / _GRID * sf / math.pow(ra, sn)
    theta = lng * deg - _OLON * deg
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    nx = int(ra * math.sin(theta) + _XO + 0.5)
    ny = int(ro - ra * math.cos(theta) + _YO + 0.5)
    return nx, ny


def base_slot(now: datetime | None = None) -> tuple[str, str]:
    """초단기실황의 발표 시각을 고른다.

    매시 정시 관측이 **약 40분 뒤에** 올라온다. 그 전에 요청하면 빈 응답이
    오므로, 40분이 지나지 않았으면 한 시간 전 것을 본다.
    """
    now = now or datetime.now()
    if now.minute < 40:
        now = now - timedelta(hours=1)
    return now.strftime("%Y%m%d"), now.strftime("%H00")


def village_slot(now: datetime | None = None) -> tuple[str, str]:
    """단기예보 발표 시각. **02·05·08·11·14·17·20·23시**에만 나온다.

    발표 후 10분쯤 지나야 조회되므로, 그 전이면 앞 회차를 본다.
    """
    now = now or datetime.now()
    hours = (2, 5, 8, 11, 14, 17, 20, 23)
    cur = now.hour - (0 if now.minute >= 10 else 1)
    for h in reversed(hours):
        if cur >= h:
            return now.strftime("%Y%m%d"), f"{h:02d}00"
    # 자정~02시10분 사이면 전날 23시 발표가 가장 최신이다.
    return (now - timedelta(days=1)).strftime("%Y%m%d"), "2300"


def _request(params: dict, url_base: str = BASE) -> dict:
    # serviceKey 는 이미 인코딩된 키가 들어오는 경우가 많아 따로 붙인다.
    key = params.pop("serviceKey")
    url = f"{url_base}?serviceKey={key}&" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SourceError(
            f"기상청 API가 오류를 돌려줬습니다(HTTP {e.code}). "
            "인증키와 활용신청 상태를 확인하세요.") from e
    except json.JSONDecodeError as e:
        # 키가 틀리면 JSON 대신 XML 오류 문서가 온다. 이걸 그대로 터뜨리면
        # 「왜 실패했는지」가 로그에 남지 않는다.
        raise SourceError(
            "기상청 API 응답을 읽지 못했습니다. 인증키가 잘못되었을 때 "
            "XML 오류 문서가 오는 경우가 있습니다.") from e
    except Exception as e:  # noqa: BLE001
        raise SourceError(
            "기상청 API에 접속하지 못했습니다. 관제망에서 외부 접속이 "
            f"막혀 있을 수 있습니다 — {str(e)[:80]}") from e


def observe(lat: float, lng: float, *, key: str | None = None,
            now: datetime | None = None) -> dict:
    """한 지점의 현재 관측값.

    돌려주는 형태 ::

        {"nx": 60, "ny": 127, "base_date": "20260816", "base_time": "1200",
         "values": {"T1H": 27.3, "RN1": 0.0, ...},
         "labels": {"기온": "27.3 ℃", ...}}

    **없는 항목은 채우지 않는다.** 0 으로 두면 「비가 안 왔다」와 「측정하지
    못했다」가 구분되지 않는다.
    """
    key = (key or api_key()).strip()
    if not key:
        raise SourceError(
            "기상청 API 키가 없습니다. 공공데이터포털에서 「기상청_단기예보 "
            "조회서비스」를 신청해 `.env` 의 KMA_API_KEY 에 넣으세요.")

    nx, ny = to_grid(lat, lng)
    base_date, base_time = base_slot(now)
    payload = _request({
        "serviceKey": key, "pageNo": 1, "numOfRows": 100,
        "dataType": "JSON", "base_date": base_date, "base_time": base_time,
        "nx": nx, "ny": ny,
    })

    header = (payload.get("response") or {}).get("header") or {}
    code = str(header.get("resultCode") or "")
    if code != "00":
        raise SourceError(
            f"기상청 API가 정상 응답이 아닙니다 — {code} "
            f"{header.get('resultMsg') or ''}")

    body = (payload.get("response") or {}).get("body") or {}
    items = ((body.get("items") or {}).get("item")) or []
    if isinstance(items, dict):
        items = [items]

    values: dict[str, float | str] = {}
    for it in items:
        cat = str(it.get("category") or "").strip()
        raw = str(it.get("obsrValue") or "").strip()
        if not cat or raw == "":
            continue
        if cat == "PTY":
            values[cat] = raw          # 코드값. 숫자로 바꾸면 의미가 사라진다
            continue
        try:
            values[cat] = float(raw)
        except ValueError:
            continue

    return {
        "nx": nx, "ny": ny,
        "base_date": base_date, "base_time": base_time,
        "values": values,
        "labels": describe(values),
    }


def forecast(lat: float, lng: float, *, key: str | None = None,
             now: datetime | None = None) -> dict:
    """단기예보에서 **오늘 하루치 요약**을 뽑는다.

    실황에 없는 것을 여기서 채운다 — **일 최고·최저기온(TMX·TMN)**,
    **신적설(SNO)**, **강수확률(POP)**. 노면 동결융해와 제설 판단에 필요하고,
    침수는 「앞으로 올 비」를 봐야 하기 때문이다.

    같은 항목이 시각마다 여러 번 오므로 **최댓값 하나로 줄인다** — 관제
    화면은 「오늘 얼마나 위험한가」를 물으므로 최악값이 맞다. 기온만은
    최고·최저를 따로 둔다.
    """
    key = (key or api_key()).strip()
    if not key:
        raise SourceError(
            "기상청 API 키가 없습니다. 공공데이터포털에서 「기상청_단기예보 "
            "조회서비스」를 신청해 `.env` 의 KMA_API_KEY 에 넣으세요.")

    nx, ny = to_grid(lat, lng)
    base_date, base_time = village_slot(now)
    payload = _request({
        "serviceKey": key, "pageNo": 1, "numOfRows": 300,
        "dataType": "JSON", "base_date": base_date, "base_time": base_time,
        "nx": nx, "ny": ny,
    }, FCST_VILLAGE)

    header = (payload.get("response") or {}).get("header") or {}
    if str(header.get("resultCode") or "") != "00":
        raise SourceError(
            f"기상청 예보 API가 정상 응답이 아닙니다 — "
            f"{header.get('resultCode')} {header.get('resultMsg') or ''}")

    body = (payload.get("response") or {}).get("body") or {}
    items = ((body.get("items") or {}).get("item")) or []
    if isinstance(items, dict):
        items = [items]

    # 오늘치만 본다. 내일 예보까지 섞으면 「오늘 위험」이 흐려진다.
    today = (now or datetime.now()).strftime("%Y%m%d")
    out: dict[str, float | str] = {}
    for it in items:
        if str(it.get("fcstDate") or "") != today:
            continue
        cat = str(it.get("category") or "").strip()
        raw = str(it.get("fcstValue") or "").strip()
        if not cat or raw == "":
            continue
        if cat in ("PTY", "SKY"):
            # 코드값은 최악을 남긴다 — 「없음(0)」이 뒤 시각 값으로 덮이면 안 된다.
            if cat not in out or str(raw) > str(out[cat]):
                out[cat] = raw
            continue
        num = _to_float(raw)
        if num is None:
            continue
        if cat == "TMN":
            out[cat] = min(num, out[cat]) if cat in out else num   # 최저는 최소
        else:
            out[cat] = max(num, out[cat]) if cat in out else num   # 나머지는 최대

    return {"nx": nx, "ny": ny, "base_date": base_date, "base_time": base_time,
            "values": out, "labels": describe(out)}


def _to_float(raw: str) -> float | None:
    """예보 강수·적설은 「강수없음」·「1.0mm 미만」처럼 **글자로 온다.**

    숫자로 못 바꾸는 값은 **버린다** — 0 으로 두면 「비가 안 온다」가 되어
    「적게 온다」와 섞인다.
    """
    s = raw.strip()
    if not s or s in ("강수없음", "적설없음", "-"):
        return 0.0
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def snapshot(lat: float, lng: float, *, key: str | None = None,
             now: datetime | None = None) -> dict:
    """실황 + 예보를 합친 한 벌. **한쪽이 실패해도 다른 쪽은 살린다.**

    관제 화면은 「기온이라도 보이는 것」이 「아무것도 없는 것」보다 낫다.
    """
    values: dict = {}
    parts: list[str] = []
    errors: list[str] = []

    try:
        obs = observe(lat, lng, key=key, now=now)
        values.update(obs["values"])
        parts.append(f'{obs["base_date"][4:6]}-{obs["base_date"][6:]} '
                     f'{obs["base_time"][:2]}시 관측')
    except SourceError as e:
        errors.append(str(e))

    try:
        fc = forecast(lat, lng, key=key, now=now)
        # 실황이 있으면 그쪽을 이긴다 — 지금 값이 예보보다 정확하다.
        for cat, v in fc["values"].items():
            values.setdefault(cat, v)
        parts.append("오늘 예보")
    except SourceError as e:
        errors.append(str(e))

    return {
        "values": values, "labels": describe(values),
        "base": " · ".join(parts),
        "errors": errors,
        "available": bool(values),
    }


def for_domain(domain: str, values: dict) -> list[dict]:
    """도메인이 실제로 보는 항목만 추려 화면에 쓰기 좋은 형태로.

    **없는 항목은 빼지 않고 「관측 없음」으로 남긴다** — 빠지면 그 항목이
    원래 필요 없는 것처럼 보인다.
    """
    out = []
    for cat in DOMAIN_CATEGORIES.get(domain, ()):
        if cat not in values:
            continue
        label, _unit = CATEGORY_LABELS.get(cat, (cat, ""))
        out.append({"code": cat, "label": label,
                    "text": describe({cat: values[cat]})[label]})
    # 같은 이름이 실황·예보 양쪽에 있으면(기온 T1H/TMP) 하나만 남긴다.
    seen, uniq = set(), []
    for row in out:
        if row["label"] in seen:
            continue
        seen.add(row["label"])
        uniq.append(row)
    return uniq


def summary_labels(values: dict) -> dict[str, str]:
    """관제 요약줄용 표기. 겹치거나 안 쓰는 항목을 뺀다(`SUMMARY_SKIP`)."""
    return describe({k: v for k, v in values.items() if k not in SUMMARY_SKIP})


def describe(values: dict) -> dict[str, str]:
    """화면에 쓸 한글 표기. 없는 항목은 넣지 않는다."""
    out: dict[str, str] = {}
    for cat, raw in values.items():
        label, unit = CATEGORY_LABELS.get(cat, (cat, ""))
        if cat == "PTY":
            out[label] = PTY_LABELS.get(str(raw), f"코드 {raw}")
            continue
        if cat == "SKY":
            out[label] = SKY_LABELS.get(str(raw), f"코드 {raw}")
            continue
        if isinstance(raw, str):        # 모르는 코드는 그대로 보여 준다
            out[label] = raw
            continue
        out[label] = f"{raw:g} {unit}".strip()
    return out


def rainfall_mm(values: dict) -> float | None:
    """1시간 강수량. **관측이 없으면 None** — 0 으로 두지 않는다."""
    v = values.get("RN1")
    return float(v) if isinstance(v, (int, float)) else None


def missing_for_prediction(values: dict) -> list[str]:
    """노면 예측 입력 중 이 API 로는 못 채우는 항목.

    「다 받았다」고 착각하지 않도록 **빠진 것을 이름으로 돌려준다.**
    """
    missing = []
    if not ("T1H" in values or "TMP" in values):
        missing.append("기온")
    if not ("RN1" in values or "PCP" in values):
        missing.append("강수량")
    if "REH" not in values:
        missing.append("습도")
    # 적설은 단기예보의 신적설(SNO)로 **부분적으로** 채워진다. 논문이 쓴
    # 「극한적설」은 관측 누적값이라 엄밀히는 다르다 — 그래서 있어도 단서를 단다.
    if "SNO" not in values:
        missing.append("적설 (단기예보 SNO 또는 종관관측 필요)")
    # 초상온도는 예보에 아예 없다. 지면 부근 최저온도라 결빙 판단의 핵심인데
    # 종관/방재기상관측(ASOS/AWS)을 따로 붙여야 한다.
    missing.append("초상온도 (종관·방재기상관측 필요)")
    return missing
