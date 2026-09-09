"""상황판 실시간 갱신 SSE (service/sse.py · /api/stream/risk).

지켜야 할 것.

* **바뀔 때만 보낸다** — 안 바뀌었는데 매초 밀면 폴링과 다를 게 없다
* **조용해도 연결은 산다** — 중간 장비가 조용한 연결을 끊는다
* **카드와 타임라인이 한 덩어리로 온다** — 따로 받으면 그 사이에 값이 바뀌어
  둘이 어긋나는 순간이 생긴다
* **권한은 /api/risk 와 같다** — 스트림으로 우회해 더 볼 수 있으면 안 된다
* **구독자 수에 상한이 있다** — 새로고침이 연결을 안 닫고 쌓이는 일이 있다
* **화면은 폴링으로 되돌아갈 수 있다** — SSE 는 개선이지 대체가 아니다
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tot_dashboard.service import sse


class FakeStore:
    """지점 상태 저장소 흉내. ``all`` 과 ``all_history`` 만 있으면 된다."""

    def __init__(self):
        self.blocks = [{"block_id": "A", "level": "관심"}]
        self.hist = {"A": ["관심"]}

    def all(self):
        return list(self.blocks)

    def all_history(self):
        return dict(self.hist)


async def _take(gen, n, *, timeout=5.0):
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(gen.__anext__(), timeout))
    return out


def test_카드와_이력이_한_덩어리로_온다():
    async def run():
        store = FakeStore()
        gen = sse.event_stream(store)
        first = (await _take(gen, 1))[0]
        await gen.aclose()
        return first

    msg = asyncio.run(run())
    assert msg.startswith("data: ")
    payload = json.loads(msg[len("data: "):].strip())
    assert "blocks" in payload and "history" in payload, (
        "카드와 타임라인을 따로 받으면 둘이 어긋나는 순간이 생긴다")
    assert payload["blocks"][0]["block_id"] == "A"


def test_바뀌지_않으면_다시_보내지_않는다(monkeypatch):
    """안 바뀌었는데 매초 밀면 폴링과 다를 게 없다."""
    monkeypatch.setattr(sse, "CHECK_SEC", 0.01)
    monkeypatch.setattr(sse, "HEARTBEAT_SEC", 999.0)   # 심박은 이 시험에서 배제

    async def run():
        store = FakeStore()
        gen = sse.event_stream(store)
        await _take(gen, 1)                 # 첫 값
        # 값이 그대로면 다음 것이 오지 않아야 한다.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gen.__anext__(), 0.2)
        await gen.aclose()

    asyncio.run(run())


def test_on_payload_콜백이_지문_계산_전에_반영된다(monkeypatch):
    """2026-08-29(R-01) — main.py::api_stream_risk()가 이 콜백으로
    침수·교통위험 whep_url을 접속 호스트 기준으로 다시 계산해 얹는다.
    콜백이 데이터를 바꾸면 그 바뀐 값 기준으로 지문(변경 여부 판단)이
    계산돼야 한다 — 안 그러면 "바뀌었을 때만 보낸다"는 판단이 콜백
    적용 전 값 기준으로 틀어진다."""
    monkeypatch.setattr(sse, "CHECK_SEC", 0.01)
    seen = []

    def _rewrite(data):
        seen.append(1)
        data["blocks"][0]["whep_url"] = "http://ug.example.internal:8889/A/whep"

    async def run():
        store = FakeStore()
        gen = sse.event_stream(store, on_payload=_rewrite)
        first = (await _take(gen, 1))[0]
        await gen.aclose()
        return first

    msg = asyncio.run(run())
    payload = json.loads(msg[len("data: "):].strip())
    assert payload["blocks"][0]["whep_url"] == "http://ug.example.internal:8889/A/whep"
    assert seen, "on_payload 콜백이 아예 안 불렸다"


def test_on_payload_없이도_기존처럼_동작한다():
    """회귀 방지 — 콜백 배선을 넣으며 기존(인자 생략) 호출이 깨지면 안
    된다."""
    async def run():
        store = FakeStore()
        gen = sse.event_stream(store)
        first = (await _take(gen, 1))[0]
        await gen.aclose()
        return first

    msg = asyncio.run(run())
    assert json.loads(msg[len("data: "):].strip())["blocks"][0]["block_id"] == "A"


def test_바뀌면_다시_보낸다(monkeypatch):
    monkeypatch.setattr(sse, "CHECK_SEC", 0.01)

    async def run():
        store = FakeStore()
        gen = sse.event_stream(store)
        await _take(gen, 1)
        store.blocks[0]["level"] = "심각"
        nxt = await asyncio.wait_for(gen.__anext__(), 2.0)
        await gen.aclose()
        return nxt

    msg = asyncio.run(run())
    assert json.loads(msg[len("data: "):].strip())["blocks"][0]["level"] == "심각"


def test_조용해도_심박이_나간다(monkeypatch):
    """중간 장비가 조용한 연결을 끊는다."""
    monkeypatch.setattr(sse, "CHECK_SEC", 0.01)
    monkeypatch.setattr(sse, "HEARTBEAT_SEC", 0.03)

    async def run():
        gen = sse.event_stream(FakeStore())
        await _take(gen, 1)
        beat = await asyncio.wait_for(gen.__anext__(), 2.0)
        await gen.aclose()
        return beat

    assert asyncio.run(run()).startswith(":"), "심박은 주석 줄이어야 한다"


def test_수집이_실패해도_스트림이_죽지_않는다(monkeypatch):
    """상황판이 죽는 것보다 다음 바퀴를 도는 편이 낫다."""
    monkeypatch.setattr(sse, "CHECK_SEC", 0.01)

    class Flaky(FakeStore):
        def __init__(self):
            super().__init__()
            self.n = 0

        def all(self):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("일시적 실패")
            return super().all()

    async def run():
        gen = sse.event_stream(Flaky())
        first = await asyncio.wait_for(gen.__anext__(), 2.0)
        await gen.aclose()
        return first

    assert asyncio.run(run()).startswith("data: ")


def test_구독자_상한을_넘으면_거절을_형식대로_알린다(monkeypatch):
    """거절도 형식을 지켜야 화면이 폴링으로 되돌아갈 수 있다."""
    monkeypatch.setattr(sse, "_clients", sse.MAX_CLIENTS)

    async def run():
        gen = sse.event_stream(FakeStore())
        msg = await asyncio.wait_for(gen.__anext__(), 2.0)
        await gen.aclose()
        return msg

    assert "event: full" in asyncio.run(run())


def test_구독자_수가_끝나면_돌아온다(monkeypatch):
    monkeypatch.setattr(sse, "CHECK_SEC", 0.01)

    async def run():
        before = sse.client_count()
        gen = sse.event_stream(FakeStore())
        await _take(gen, 1)
        during = sse.client_count()
        await gen.aclose()
        return before, during, sse.client_count()

    before, during, after = asyncio.run(run())
    assert during == before + 1
    assert after == before, "연결을 닫았는데 구독자 수가 줄지 않으면 샌다"


def test_프록시_버퍼링을_끄는_헤더가_있다():
    """버퍼링되면 밀어 주는 의미가 사라진다."""
    assert sse.HEADERS.get("X-Accel-Buffering") == "no"
    assert sse.HEADERS.get("Cache-Control") == "no-cache"


def test_스트림_권한이_등록돼_있다():
    """/api/ 는 fail-closed 라 등록하지 않으면 막힌다."""
    from tot_dashboard.core import guard

    assert guard._match("GET", "/api/stream/risk") is not None, (
        "스트림 경로가 권한 규칙에 없다 — 로그인해도 막힌다")


# ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 아래 끝단(E2E) 시험은
# `/api/stream/risk`가 traffic-service로 옮겨가며 그쪽 앱을 대상으로
# 한다. 위쪽 sse.py 단위 시험(FakeStore 기반)은 도메인 무관 순수
# 배관이라 그대로 둔다.


@pytest.fixture(scope="module")
def anon_client():
    from fastapi.testclient import TestClient

    from tot_dashboard.service.traffic_service import app
    return TestClient(app)


@pytest.fixture(scope="module")
def client(anon_client, seeded_users, login_cross_service):
    login_cross_service(anon_client, *seeded_users["admin"])
    yield anon_client
    anon_client.cookies.clear()


def test_로그인하지_않으면_스트림도_막힌다(anon_client, db_schema):
    anon_client.cookies.clear()
    r = anon_client.get("/api/stream/risk")
    assert r.status_code in (401, 403), (
        f"인증 없이 스트림이 열렸다: {r.status_code}")


def test_끝단까지_SSE_형식으로_나온다(client, db_schema, monkeypatch):
    """경로·헤더·본문 형식을 끝단에서 확인한다.

    ⚠️ **정상 스트림은 끝나지 않는다.** 시험에서 그대로 읽으면 응답이 완결되지
    않아 멈춘다(실제로 걸렸다). 그래서 **구독 상한을 0으로 낮춰** 서버가
    거절 이벤트를 보내고 **끝내도록** 만든 뒤 형식을 본다. 거절도 SSE 형식을
    지켜야 화면이 폴링으로 되돌아갈 수 있으므로, 이 경로를 확인하는 것 자체가
    의미가 있다.
    """
    monkeypatch.setattr(sse, "MAX_CLIENTS", 0)
    r = client.get("/api/stream/risk")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert r.headers.get("x-accel-buffering") == "no", (
        "프록시가 버퍼링하면 밀어 주는 의미가 사라진다")
    assert r.headers.get("cache-control") == "no-cache"
    assert "event: full" in r.text


def test_스트림_권한이_위험도_조회와_같다():
    """스트림으로 우회해 더 볼 수 있으면 안 된다."""
    from tot_dashboard.core import guard

    a = guard._match("GET", "/api/stream/risk")
    b = guard._match("GET", "/api/risk")
    assert a == b, f"권한이 다르다: 스트림 {a} / 조회 {b}"
