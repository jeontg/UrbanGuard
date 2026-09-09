"""교통 CCTV 목록 수집 — 지역 프리셋과 해상도 처리.

실제 API 는 키가 필요하므로 응답만 흉내 내어 **거르기·정렬 규칙**을 본다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import cctv_sources as SRC


# ---------- 해상도 문자열 읽기 ----------

@pytest.mark.parametrize("text,want", [
    ("1920x1080", (1920, 1080)),
    ("1280*720", (1280, 720)),
    ("1920X1080", (1920, 1080)),
    (" 3840x2160 ", (3840, 2160)),
    ("FHD", (1920, 1080)),
    ("hd", (1280, 720)),
    ("4K", (3840, 2160)),
])
def test_해상도_표기를_읽는다(text, want):
    assert SRC.parse_resolution(text) == want


@pytest.mark.parametrize("text", ["", "   ", "미상", "1920x", "x1080", "0x0", None])
def test_못_읽으면_None이지_0이_아니다(text):
    """0 으로 두면 「저해상도」로 잘못 걸러진다."""
    assert SRC.parse_resolution(text) is None


# ---------- 지역 프리셋 ----------

def test_전국_17개_시도가_있다():
    assert len(SRC.REGIONS) == 17
    for key in ("busan", "ulsan", "gyeongnam", "seoul", "jeju", "gangwon"):
        assert key in SRC.REGIONS


@pytest.mark.parametrize("name,lat,lng,want", [
    ("부산역", 35.115, 129.042, "busan"),
    ("서울시청", 37.566, 126.978, "seoul"),
    ("수원", 37.263, 127.029, "gyeonggi"),
    ("대구", 35.871, 128.601, "daegu"),
    ("제주", 33.499, 126.531, "jeju"),
])
def test_겹치는_사각형에서_더_좁은_쪽을_고른다(name, lat, lng, want):
    """서울은 경기 안에, 부산은 경남 안에 든다. 좁을수록 구체적이다."""
    assert SRC.region_of(lat, lng) == want, name


@pytest.mark.parametrize("lat,lng", [
    (35.68, 139.69),    # 도쿄
    (None, None),
    ("숫자아님", 127.0),
])
def test_판정_못_하면_None(lat, lng):
    assert SRC.region_of(lat, lng) is None


def test_모르는_지역은_지역_미상으로_보여_준다():
    """빈 이름으로 두면 화면에서 보이지 않아 해지도 못 한다."""
    assert SRC.region_label(None) == "지역 미상"
    assert SRC.region_label("") == "지역 미상"
    assert SRC.region_label("없는키") == "지역 미상"
    assert SRC.region_label("busan") == "부산광역시"


def test_모든_경계가_국내_좌표_범위_안이다():
    """좌표 검증(위도 33~39 / 경도 124~132)과 어긋나면 API 가 거부한다."""
    for key, (label, bbox) in SRC.REGIONS.items():
        assert 124 <= bbox["minX"] < bbox["maxX"] <= 132, key
        assert 33 <= bbox["minY"] < bbox["maxY"] <= 39, key
        assert label.strip(), key


# ---------- 수집 결과 거르기·정렬 ----------

def _fake_payload(rows):
    return {"response": {"data": rows}}


def _row(name, res, *, url=None):
    return {"cctvname": name, "cctvurl": url or f"https://x/{name}.m3u8",
            "coordx": 129.0, "coordy": 35.1, "cctvresolution": res}


@pytest.fixture
def _api(monkeypatch):
    """API 호출을 가로채 준비한 줄만 돌려준다. 첫 유형에만 담아 중복을 피한다."""
    def install(rows):
        calls = {"n": 0}

        def fake(params):
            calls["n"] += 1
            return _fake_payload(rows if calls["n"] == 1 else [])

        monkeypatch.setattr(SRC, "_request", fake)
        monkeypatch.setenv("ITS_API_KEY", "테스트키")
    return install


def test_해상도_높은_순으로_정렬한다(_api):
    _api([_row("가", "1280x720"), _row("나", "1920x1080"), _row("다", "640x480")])
    got = [x["name"] for x in SRC.fetch("seoul")]
    assert got == ["나", "가", "다"]


def test_표기가_없는_지점은_뒤로_가되_사라지지_않는다(_api):
    _api([_row("표기없음", ""), _row("FHD지점", "1920x1080")])
    got = SRC.fetch("jeju")
    assert [x["name"] for x in got] == ["FHD지점", "표기없음"]
    assert got[1]["height"] is None


def test_min_height로_저해상도를_뺀다(_api):
    _api([_row("SD", "640x480"), _row("HD", "1280x720"), _row("FHD", "1920x1080")])
    got = [x["name"] for x in SRC.fetch("daegu", min_height=720)]
    assert got == ["FHD", "HD"]


def test_min_height를_줘도_표기없는_지점은_남긴다(_api):
    """표기가 빈 지점을 버리면 실제 고해상도까지 함께 사라진다."""
    _api([_row("SD", "640x480"), _row("모름", "")])
    got = [x["name"] for x in SRC.fetch("gwangju", min_height=1080)]
    assert got == ["모름"]


def test_모르는_지역은_거부한다(monkeypatch):
    monkeypatch.setenv("ITS_API_KEY", "테스트키")
    with pytest.raises(SRC.SourceError, match="알 수 없는 지역"):
        SRC.fetch("도쿄")


def test_키가_없으면_안내한다(monkeypatch):
    monkeypatch.delenv("ITS_API_KEY", raising=False)
    monkeypatch.delenv("BUSAN_API_KEY", raising=False)
    with pytest.raises(SRC.SourceError, match="API 키가 없습니다"):
        SRC.fetch("seoul")
