"""S-01 기상 카드와 S-61 노면 간극 카드.

지켜야 할 것.

* **기상을 못 가져와도 상황판은 뜬다** — 관제가 기상 때문에 멈추면 안 된다
* **키가 없는 것은 고장이 아니다** — 안내만 하고 넘어간다
* **못 채운 예측 입력을 숨기지 않는다** — 「다 받았다」고 오해하면 안 된다
* **S-61 이 실무 기준과의 간극을 숫자로 말한다** — 보정 현황은 지금 상태에서 센다
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tot_dashboard.core import weather_sources as W
from tot_dashboard.service import main as M
from tot_dashboard.service.main import app


@pytest.fixture(scope="module")
def anon_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login):
    login(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


@pytest.fixture(autouse=True)
def _clear_cache():
    """캐시가 남으면 다음 시험이 앞 시험의 응답을 본다."""
    M._weather_cache.clear()
    yield
    M._weather_cache.clear()


# ---------- S-01 기상 ----------

def test_키가_없어도_상황판은_뜬다(client, monkeypatch):
    monkeypatch.delenv("KMA_API_KEY", raising=False)
    monkeypatch.delenv("WEATHER_API_KEY", raising=False)
    r = client.get("/")
    assert r.status_code == 200
    assert "미연계" in r.text
    assert "API 키가 없습니다" in r.text


def test_기상_조회가_터져도_상황판은_뜬다(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("네트워크 끊김")

    monkeypatch.setattr(W, "snapshot", boom)
    r = client.get("/")
    assert r.status_code == 200
    assert "가져오지 못했습니다" in r.text


def _snap(values, labels, base="08-16 13시 관측", errors=()):
    return {"values": values, "labels": labels, "base": base,
            "errors": list(errors), "available": bool(values)}


def test_관측값이_있으면_화면에_나온다(client, monkeypatch):
    monkeypatch.setattr(W, "snapshot", lambda lat, lng, **k: _snap(
        {"T1H": 27.3, "RN1": 2.5, "REH": 80.0},
        {"기온": "27.3 ℃", "1시간 강수량": "2.5 mm", "습도": "80 %"}))
    r = client.get("/")
    assert r.status_code == 200
    assert "27.3 ℃" in r.text
    assert "2.5 mm" in r.text
    assert "08-16 13시 관측" in r.text


def test_도메인마다_보는_항목이_나뉘어_나온다(client, monkeypatch):
    """전부 늘어놓으면 관제요원이 무엇을 봐야 하는지 알 수 없다."""
    monkeypatch.setattr(W, "snapshot", lambda lat, lng, **k: _snap(
        {"T1H": 27.3, "RN1": 2.5, "REH": 80.0, "POP": 60.0,
         "WSD": 3.2, "SNO": 0.0, "TMX": 31.0, "TMN": 24.0},
        {"기온": "27.3 ℃"}))
    html = client.get("/").text
    for key in ("침수", "인파", "노면", "강수확률", "풍속", "1시간 신적설"):
        assert key in html, key
    # 「왜 보는지」가 함께 있어야 숫자가 뜻을 갖는다
    assert "선행 지표" in html


def test_못_채운_예측_입력을_밝힌다(client, monkeypatch):
    """초상온도는 예보에도 없다. 숨기면 다 받은 줄 안다."""
    monkeypatch.setattr(W, "snapshot", lambda lat, lng, **k: _snap(
        {"T1H": 20.0}, {"기온": "20 ℃"}))
    r = client.get("/")
    assert "초상온도" in r.text


def test_한쪽만_실패하면_그것도_알려_준다(client, monkeypatch):
    monkeypatch.setattr(W, "snapshot", lambda lat, lng, **k: _snap(
        {"T1H": 20.0}, {"기온": "20 ℃"}, errors=["예보를 받지 못했습니다"]))
    r = client.get("/")
    assert "일부만 받았습니다" in r.text
    assert "예보를 받지 못했습니다" in r.text


def test_같은_조건이면_다시_묻지_않는다(client, monkeypatch):
    """관측이 10분 간격이라 더 자주 물어도 값이 같고, 한도는 1만 회/일이다."""
    calls = {"n": 0}

    def counted(lat, lng, **k):
        calls["n"] += 1
        return _snap({"T1H": 20.0}, {"기온": "20 ℃"})

    monkeypatch.setattr(W, "snapshot", counted)
    client.get("/")
    client.get("/")
    client.get("/")
    assert calls["n"] == 1


def test_실패도_캐시한다_반복_요청을_막는다(client, monkeypatch):
    calls = {"n": 0}

    def failing(lat, lng, **k):
        calls["n"] += 1
        return _snap({}, {}, errors=["키 없음"])

    monkeypatch.setattr(W, "snapshot", failing)
    client.get("/")
    client.get("/")
    assert calls["n"] == 1


# ---------- S-61 노면 간극 ----------

def test_S61에_기관_기준과의_간극이_나온다(client):
    html = client.get("/models").text
    assert "기관 기준과의 간극" in html
    for key in ("균열률", "소성변형", "평탄성", "서울형 Decision Tree"):
        assert key in html, key


def test_측정_못_하는_항목을_밝힌다(client):
    """RD·IRI 는 CCTV 로 못 잰다. 표에 빈칸으로 두면 곧 채워질 것처럼 보인다."""
    html = client.get("/models").text
    assert "측정 불가" in html


def test_구간_보정_현황을_숫자로_보여_준다(client):
    html = client.get("/models").text
    assert "구간 길이 보정" in html
    assert "개소" in html


def test_제안서_문구_주의가_붙어_있다(client):
    """「탐지된다」와 「기관 기준으로 판정한다」는 다른 말이다."""
    html = client.get("/models").text
    assert "제안서 문구 주의" in html


def test_낡은_안내문이_사라졌다(client):
    """공개데이터 확보에 실패했다는 서술은 SVRDD 확보로 사실이 아니게 됐다."""
    html = client.get("/models").text
    assert "모두 실패해" not in html
