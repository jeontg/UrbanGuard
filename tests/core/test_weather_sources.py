"""기상청 단기예보 연계 — 격자 변환·발표시각·응답 해석.

지켜야 할 것.

* **격자 변환이 정확해야 한다** — 틀리면 엉뚱한 지역 날씨를 받아 온다
* **관측 없음을 0 으로 두지 않는다** — 「비가 안 왔다」와 구분되어야 한다
* **HTTP 200 이어도 resultCode 를 본다** — 기상청은 오류도 200 으로 준다
* **못 채운 항목을 이름으로 알려 준다** — 「다 받았다」고 착각하면 안 된다
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tot_dashboard.core import weather_sources as W


# ---------- 격자 변환 ----------

def test_서울시청이_기상청_예시_격자와_같다():
    """기상청이 공개한 예시가 (60, 127) 이다. 여기가 어긋나면 전부 어긋난다."""
    assert W.to_grid(37.5665, 126.9780) == (60, 127)


@pytest.mark.parametrize("lat,lng", [
    (35.1796, 129.0756),   # 부산
    (33.4996, 126.5312),   # 제주
    (37.4563, 126.7052),   # 인천
])
def test_국내_좌표는_격자_범위_안에_든다(lat, lng):
    """전국 격자는 동서 149 x 남북 253 이다."""
    nx, ny = W.to_grid(lat, lng)
    assert 1 <= nx <= 149
    assert 1 <= ny <= 253


def test_다른_지점은_다른_격자를_준다():
    assert W.to_grid(37.5665, 126.9780) != W.to_grid(35.1796, 129.0756)


# ---------- 발표 시각 ----------

def test_40분_전이면_한_시간_전_발표를_본다():
    """정시 관측이 약 40분 뒤에 올라온다. 그 전에 요청하면 빈 응답이 온다."""
    assert W.base_slot(datetime(2026, 8, 16, 13, 10)) == ("20260816", "1200")


def test_40분_뒤면_같은_시간_발표를_본다():
    assert W.base_slot(datetime(2026, 8, 16, 13, 55)) == ("20260816", "1300")


def test_자정_직후에는_전날로_넘어간다():
    assert W.base_slot(datetime(2026, 8, 16, 0, 5)) == ("20260815", "2300")


# ---------- 키 없음 ----------

def test_키가_없으면_안내한다(monkeypatch):
    monkeypatch.delenv("KMA_API_KEY", raising=False)
    monkeypatch.delenv("WEATHER_API_KEY", raising=False)
    with pytest.raises(W.SourceError, match="API 키가 없습니다"):
        W.observe(35.1796, 129.0756)


# ---------- 응답 해석 ----------

def _ok(items):
    return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                         "body": {"items": {"item": items}}}}


def _item(cat, val):
    return {"category": cat, "obsrValue": val}


def test_관측값을_숫자로_읽는다(monkeypatch):
    monkeypatch.setattr(W, "_request", lambda p: _ok([
        _item("T1H", "27.3"), _item("RN1", "0"), _item("REH", "65")]))
    got = W.observe(37.5665, 126.9780, key="테스트키")
    assert got["values"]["T1H"] == pytest.approx(27.3)
    assert got["values"]["REH"] == pytest.approx(65.0)
    assert got["nx"] == 60 and got["ny"] == 127


def test_강수형태는_코드_그대로_둔다(monkeypatch):
    """숫자로 바꾸면 「없음(0)」과 결측이 섞인다."""
    monkeypatch.setattr(W, "_request", lambda p: _ok([_item("PTY", "1")]))
    got = W.observe(37.5, 127.0, key="테스트키")
    assert got["values"]["PTY"] == "1"
    assert got["labels"]["강수형태"] == "비"


def test_빈_값은_버린다(monkeypatch):
    monkeypatch.setattr(W, "_request", lambda p: _ok([
        _item("T1H", ""), _item("RN1", "1.5")]))
    got = W.observe(37.5, 127.0, key="테스트키")
    assert "T1H" not in got["values"]
    assert got["values"]["RN1"] == pytest.approx(1.5)


def test_resultCode_가_00_이_아니면_실패로_본다(monkeypatch):
    """기상청은 오류도 HTTP 200 으로 준다."""
    monkeypatch.setattr(W, "_request", lambda p: {
        "response": {"header": {"resultCode": "03",
                                "resultMsg": "NO_DATA"}}})
    with pytest.raises(W.SourceError, match="정상 응답이 아닙니다"):
        W.observe(37.5, 127.0, key="테스트키")


def test_항목이_하나면_dict_로_와도_읽는다(monkeypatch):
    """응답이 배열이 아니라 객체 하나로 오는 경우가 있다."""
    monkeypatch.setattr(W, "_request",
                        lambda p: _ok(_item("T1H", "20.0")))
    got = W.observe(37.5, 127.0, key="테스트키")
    assert got["values"]["T1H"] == pytest.approx(20.0)


# ---------- 강수량과 결측 ----------

def test_강수량이_없으면_None(monkeypatch):
    """0 으로 두면 「비가 안 왔다」와 구분되지 않는다."""
    assert W.rainfall_mm({}) is None
    assert W.rainfall_mm({"T1H": 20.0}) is None


def test_강수량이_0이면_0():
    assert W.rainfall_mm({"RN1": 0.0}) == 0.0


def test_예측_입력_중_못_채운_것을_알려_준다():
    """실황만 있으면 적설이 빠진다. 초상온도는 예보에도 없다."""
    missing = W.missing_for_prediction({"T1H": 20.0, "RN1": 0.0, "REH": 60.0})
    assert any("적설" in m for m in missing)
    assert any("초상온도" in m for m in missing)
    assert not any(m == "기온" for m in missing)


def test_신적설을_받으면_적설은_빠진다():
    """단기예보 SNO 로 부분적으로 채워진다."""
    missing = W.missing_for_prediction(
        {"TMP": 20.0, "PCP": 0.0, "REH": 60.0, "SNO": 0.0})
    assert not any("적설" in m for m in missing)
    assert any("초상온도" in m for m in missing)   # 이건 여전히 없다


# ---------- 단기예보 ----------

@pytest.mark.parametrize("h,m,want", [
    (2, 20, ("20260816", "0200")),
    (13, 0, ("20260816", "1100")),
    (14, 15, ("20260816", "1400")),
    (23, 50, ("20260816", "2300")),
])
def test_단기예보는_정해진_시각에만_나온다(h, m, want):
    """02·05·08·11·14·17·20·23시 발표. 그 사이면 앞 회차를 본다."""
    assert W.village_slot(datetime(2026, 8, 16, h, m)) == want


def test_새벽에는_전날_23시_발표를_본다():
    assert W.village_slot(datetime(2026, 8, 16, 1, 30)) == ("20260815", "2300")
    assert W.village_slot(datetime(2026, 8, 16, 2, 5)) == ("20260815", "2300")


@pytest.mark.parametrize("raw,want", [
    ("강수없음", 0.0),
    ("적설없음", 0.0),
    ("1.0mm 미만", 1.0),
    ("2.5", 2.5),
    ("30.0~50.0mm", 30.0),
])
def test_글자로_오는_예보값을_숫자로_읽는다(raw, want):
    """예보 강수·적설은 「강수없음」처럼 글자로 온다."""
    assert W._to_float(raw) == pytest.approx(want)


def test_못_읽는_예보값은_버린다():
    """0 으로 두면 「비가 안 온다」가 되어 「적게 온다」와 섞인다."""
    assert W._to_float("알수없음") is None


def test_예보는_오늘치만_본다(monkeypatch):
    """내일 예보까지 섞으면 「오늘 위험」이 흐려진다."""
    def fake(params, url_base=None):
        return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [
            {"fcstDate": "20260816", "category": "TMX", "fcstValue": "31.0"},
            {"fcstDate": "20260817", "category": "TMX", "fcstValue": "38.0"},
        ]}}}}

    monkeypatch.setattr(W, "_request", fake)
    got = W.forecast(37.5, 127.0, key="테스트키",
                     now=datetime(2026, 8, 16, 14, 30))
    assert got["values"]["TMX"] == pytest.approx(31.0)   # 내일 38 이 아니다


def test_같은_항목이_여러_시각에_오면_최악값을_남긴다(monkeypatch):
    def fake(params, url_base=None):
        return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [
            {"fcstDate": "20260816", "category": "POP", "fcstValue": "20"},
            {"fcstDate": "20260816", "category": "POP", "fcstValue": "80"},
            {"fcstDate": "20260816", "category": "TMN", "fcstValue": "24"},
            {"fcstDate": "20260816", "category": "TMN", "fcstValue": "21"},
        ]}}}}

    monkeypatch.setattr(W, "_request", fake)
    got = W.forecast(37.5, 127.0, key="테스트키",
                     now=datetime(2026, 8, 16, 14, 30))
    assert got["values"]["POP"] == pytest.approx(80.0)   # 최대
    assert got["values"]["TMN"] == pytest.approx(21.0)   # 최저는 최소


# ---------- 합치기 ----------

def test_한쪽이_실패해도_다른_쪽은_살린다(monkeypatch):
    """기온이라도 보이는 것이 아무것도 없는 것보다 낫다."""
    monkeypatch.setattr(W, "observe", lambda *a, **k: {
        "base_date": "20260816", "base_time": "1300",
        "values": {"T1H": 27.0}, "labels": {"기온": "27 ℃"}})

    def boom(*a, **k):
        raise W.SourceError("예보 실패")

    monkeypatch.setattr(W, "forecast", boom)
    snap = W.snapshot(37.5, 127.0, key="테스트키")
    assert snap["available"]
    assert snap["values"]["T1H"] == pytest.approx(27.0)
    assert snap["errors"]


def test_실황이_예보를_이긴다(monkeypatch):
    """지금 값이 예보보다 정확하다."""
    monkeypatch.setattr(W, "observe", lambda *a, **k: {
        "base_date": "20260816", "base_time": "1300",
        "values": {"REH": 80.0}, "labels": {}})
    monkeypatch.setattr(W, "forecast", lambda *a, **k: {
        "base_date": "20260816", "base_time": "1100",
        "values": {"REH": 55.0, "TMX": 31.0}, "labels": {}})
    snap = W.snapshot(37.5, 127.0, key="테스트키")
    assert snap["values"]["REH"] == pytest.approx(80.0)    # 실황
    assert snap["values"]["TMX"] == pytest.approx(31.0)    # 예보에만 있는 것


def test_둘_다_실패하면_available_이_False(monkeypatch):
    def boom(*a, **k):
        raise W.SourceError("키 없음")

    monkeypatch.setattr(W, "observe", boom)
    monkeypatch.setattr(W, "forecast", boom)
    snap = W.snapshot(37.5, 127.0, key="")
    assert snap["available"] is False
    assert len(snap["errors"]) == 2


# ---------- 도메인별 ----------

VALUES = {"T1H": 27.3, "RN1": 2.5, "REH": 80.0, "PTY": "1", "SKY": "4",
          "TMX": 31.0, "TMN": 24.0, "SNO": 0.0, "POP": 60.0, "WSD": 3.2}


@pytest.mark.parametrize("domain,want", [
    ("flood", "강수확률"),
    ("crowd", "풍속"),
    ("road", "1시간 신적설"),
])
def test_도메인마다_보는_항목이_다르다(domain, want):
    labels = [i["label"] for i in W.for_domain(domain, VALUES)]
    assert want in labels


def test_침수는_풍속을_보지_않는다():
    """전부 늘어놓으면 무엇을 봐야 하는지 알 수 없다."""
    labels = [i["label"] for i in W.for_domain("flood", VALUES)]
    assert "풍속" not in labels
    assert "1시간 신적설" not in labels


def test_기온이_실황_예보_양쪽에_있어도_한_번만_나온다():
    vals = dict(VALUES, TMP=28.0)          # T1H 와 TMP 가 둘 다 「기온」
    labels = [i["label"] for i in W.for_domain("road", vals)]
    assert labels.count("기온") == 1


def test_모르는_도메인은_빈_목록():
    assert W.for_domain("없는도메인", VALUES) == []


def test_요약줄에서_겹치는_항목을_뺀다():
    """UUU·VVV 는 바람 성분이라 풍속·풍향과 겹치고, 파고는 쓰지 않는다."""
    vals = {"T1H": 25.0, "WSD": 2.4, "VEC": 122.0,
            "UUU": -2.0, "VVV": 1.3, "WAV": 0.0}
    labels = W.summary_labels(vals)
    assert "기온" in labels
    assert "풍속" in labels and "풍향" in labels
    for gone in ("UUU", "VVV", "파고"):
        assert gone not in labels


def test_숨기는_것이지_버리는_것이_아니다():
    """describe 는 그대로 전부 보여 준다 — 필요해지면 꺼내 쓸 수 있어야 한다."""
    full = W.describe({"UUU": -2.0, "WAV": 0.5})
    assert "UUU" in full and "파고" in full


def test_하늘상태를_한글로_보여_준다():
    assert W.describe({"SKY": "4"})["하늘상태"] == "흐림"
    assert W.describe({"SKY": "9"})["하늘상태"] == "코드 9"


def test_아무것도_못_받으면_전부_빠진_것으로_센다():
    missing = W.missing_for_prediction({})
    for name in ("기온", "강수량", "습도"):
        assert name in missing
