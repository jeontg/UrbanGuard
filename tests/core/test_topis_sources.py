"""서울 TOPIS 수집 — 주소 고르기와 정규화.

지켜야 할 것.

* **화질 높은 주소를 고른다** — spatic 쪽이 실측에서 같거나 더 높았다
* **서울 밖 좌표는 버린다** — 지도에 엉뚱하게 찍히면 관제가 헷갈린다
* **해상도를 지어내지 않는다** — API가 안 주므로 모른다고 둔다
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import topis_sources as T


# ---------- 재생 주소 고르기 ----------

def test_spatic를_우선한다():
    """같은 카메라가 자체 720x480 · spatic 1280x720 이었다."""
    info = {"hlsUrl": "https://topiscctv1.eseoul.go.kr/sd1/ch1.stream/playlist.m3u8",
            "remark5": "https://strm1.spatic.go.kr:443/live/1.stream/playlist.m3u8"}
    assert T.stream_url(info).startswith("https://strm1.spatic.go.kr")


def test_spatic가_없으면_자체_주소로_내려간다():
    info = {"hlsUrl": "https://topiscctv1.eseoul.go.kr/sd1/ch1.stream/playlist.m3u8",
            "remark5": ""}
    assert T.stream_url(info).startswith("https://topiscctv1.eseoul.go.kr")


def test_쓸_수_있는_주소가_없으면_빈_문자열():
    assert T.stream_url({}) == ""
    assert T.stream_url({"remark5": "rtsp://10.1.120.34:554/x"}) == ""


# ---------- 정규화 ----------

def _cam(lat=37.5665, lng=126.9780, name="시청앞", cam_id="1"):
    return {"camId": cam_id, "camName": name, "lat": lat, "lng": lng}


def _info(url="https://strm1.spatic.go.kr:443/live/1.stream/playlist.m3u8"):
    return {"remark5": url}


def test_정상_한_줄을_등록_형태로():
    item = T._norm(_cam(), _info())
    assert item["name"] == "시청앞"
    assert item["source_type"] == "hls"
    assert item["cam_id"] == "1"
    assert item["lat"] == pytest.approx(37.5665)


def test_해상도는_모른다고_둔다():
    """API가 알려주지 않는다. 0으로 채우면 저해상도로 잘못 걸러진다."""
    item = T._norm(_cam(), _info())
    assert item["width"] is None
    assert item["height"] is None
    assert item["resolution"] == ""


@pytest.mark.parametrize("lat,lng", [
    (35.1796, 129.0756),   # 부산
    (37.5665, 139.0),      # 경도 밖
    (0.0, 0.0),
])
def test_서울_밖_좌표는_버린다(lat, lng):
    assert T._norm(_cam(lat=lat, lng=lng), _info()) is None


@pytest.mark.parametrize("bad", [
    {"camId": "1", "camName": "", "lat": 37.5, "lng": 127.0},
    {"camId": "1", "camName": "이름", "lat": None, "lng": 127.0},
    {"camId": "1", "camName": "이름", "lat": "숫자아님", "lng": 127.0},
])
def test_쓸_수_없는_줄은_None(bad):
    assert T._norm(bad, _info()) is None


def test_주소가_없으면_None():
    assert T._norm(_cam(), {}) is None


# ---------- 목록 수집 ----------

def test_목록은_요청한_수만큼만(monkeypatch):
    """510개소를 다 긁으면 상대 서버에 부담이다."""
    def fake_post(url, params):
        page = int(params["pageIndex"])
        return {"TotalRows": 100, "rows": [
            {"camId": str(page * 10 + i), "camName": f"지점{page}-{i}",
             "lat": 37.5, "lng": 127.0} for i in range(10)]}

    monkeypatch.setattr(T, "_post", fake_post)
    monkeypatch.setattr(T.time, "sleep", lambda _s: None)
    assert len(T.fetch_list(max_count=25)) == 25


def test_빈_쪽이_나오면_멈춘다(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, params):
        calls["n"] += 1
        return {"TotalRows": 999, "rows": [] if calls["n"] > 1 else [
            {"camId": "1", "camName": "가", "lat": 37.5, "lng": 127.0}]}

    monkeypatch.setattr(T, "_post", fake_post)
    monkeypatch.setattr(T.time, "sleep", lambda _s: None)
    assert len(T.fetch_list(max_count=100)) == 1
    assert calls["n"] == 2      # 빈 쪽에서 멈췄다


def test_같은_주소는_한_번만(monkeypatch):
    """두 지점이 같은 스트림을 가리키면 중복 등록이 된다."""
    same = "https://strm1.spatic.go.kr:443/live/9.stream/playlist.m3u8"
    monkeypatch.setattr(T, "fetch_list", lambda **k: [
        _cam(name="가", cam_id="1"), _cam(name="나", cam_id="2")])
    monkeypatch.setattr(T, "fetch_info", lambda cid: _info(same))
    monkeypatch.setattr(T.time, "sleep", lambda _s: None)
    assert len(T.fetch(max_count=10)) == 1


def test_이름으로_거를_수_있다(monkeypatch):
    monkeypatch.setattr(T, "fetch_list", lambda **k: [
        _cam(name="(남산)한옥마을", cam_id="1"),
        _cam(name="강남역", cam_id="2")])
    monkeypatch.setattr(
        T, "fetch_info",
        lambda cid: _info(f"https://strm1.spatic.go.kr:443/live/{cid}.stream/playlist.m3u8"))
    monkeypatch.setattr(T.time, "sleep", lambda _s: None)
    got = T.fetch(max_count=10, name_filter="남산")
    assert [x["name"] for x in got] == ["(남산)한옥마을"]
