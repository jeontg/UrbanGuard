"""상황판 실시간 갱신 — Server-Sent Events (S-01).

왜 바꾸나
    화면이 **1초마다 ``/api/risk`` 와 ``/api/history`` 를 물어보고** 있었다.
    관제 PC 한 대가 하루에 17만 번 넘게 묻는 셈인데, 그중 대부분은
    **바뀐 것이 없다**는 답이다. 게다가 갱신 지연이 폴링 주기에 묶여
    「이미 바뀐 값을 최대 1초 뒤에」 본다.

    SSE 는 **바뀔 때만 서버가 밀어 준다.** 단방향이라 WebSocket 처럼 양쪽
    상태를 관리할 필요가 없고, **추가 의존성이 하나도 없다** — 망분리
    환경에서 설치 대상을 늘리지 않는 것이 성능보다 중요하다.

무엇을 조심했나
    * **연결이 끊기면 화면이 멈춘다.** 그래서 화면 쪽에 폴링 되돌리기를
      남겨 두었다. SSE 는 개선이지 대체가 아니다
    * **바뀌지 않아도 살아 있음을 알려야 한다.** 중간 장비가 조용한 연결을
      끊는다. 주석 줄(``:``)로 심박을 보낸다
    * **연결마다 스레드를 잡지 않는다.** 39지점을 보는 관제 PC 가 여럿이면
      금방 늘어난다. 비동기 제너레이터로 흘린다
"""
from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("urbanguard.sse")

# 바뀐 것이 없나 확인하는 주기. 폴링 주기(1초)와 같게 두되, **바뀌었을 때만**
# 보내므로 실제 전송량은 크게 줄어든다.
CHECK_SEC = 1.0

# 심박 주기. 중간 장비가 조용한 연결을 끊는 것을 막는다.
HEARTBEAT_SEC = 15.0

# 한 번에 붙을 수 있는 구독자 수. 관제 PC 가 아무리 많아도 이보다 많으면
# 무언가 잘못된 것이다(새로고침이 연결을 안 닫고 쌓이는 등).
MAX_CLIENTS = 32

_clients = 0


def client_count() -> int:
    return _clients


def _payload(store) -> dict:
    """화면이 한 번에 필요한 것을 묶는다.

    폴링일 때는 ``/api/risk`` 와 ``/api/history`` 를 **따로** 불렀다. 두 번
    왕복하면 그 사이에 값이 바뀌어 **카드와 타임라인이 어긋나는** 순간이
    생긴다. 한 번에 보내면 그 틈이 없다.
    """
    return {"blocks": store.all(), "history": store.all_history()}


def _fingerprint(data: dict) -> str:
    """바뀌었는지 가리는 값.

    전체를 문자열로 만들어 비교한다. 지점 39개 규모에서는 이 비용이
    네트워크로 매번 밀어 보내는 것보다 훨씬 싸다.
    """
    return json.dumps(data, sort_keys=True, default=str)


async def event_stream(store, on_payload=None):
    """SSE 본문. 바뀌었을 때만 ``data:`` 를 보낸다.

    ``on_payload`` — 2026-08-29(R-01). 이 모듈은 도메인 지식이 없는
    순수 전송 배관이다. 침수·교통위험의 WHEP 주소를 접속 호스트 기준
    으로 다시 계산하는 것 같은 도메인 로직은 여기 넣지 않고, 호출부
    (``main.py::api_stream_risk()``)가 이 콜백으로 ``_payload()`` 직후·
    지문 계산 직전에 얹는다 — 지문도 갱신된 값 기준으로 계산돼야
    "바뀌었을 때만 보낸다"는 판단이 정확하다.
    """
    global _clients
    if _clients >= MAX_CLIENTS:
        # 거절도 형식을 지켜서 알린다 — 화면이 폴링으로 되돌아갈 수 있게.
        yield "event: full\ndata: {}\n\n"
        return

    _clients += 1
    last = None
    quiet = 0.0
    try:
        while True:
            try:
                data = _payload(store)
                if on_payload:
                    on_payload(data)
                mark = _fingerprint(data)
            except Exception as e:  # noqa: BLE001
                # 상황판이 죽는 것보다 조용히 넘기고 다음 바퀴를 도는 편이 낫다.
                log.warning("상황판 스트림 수집 실패: %s", str(e)[:120])
                await asyncio.sleep(CHECK_SEC)
                continue

            if mark != last:
                last = mark
                quiet = 0.0
                yield f"data: {json.dumps(data, default=str)}\n\n"
            else:
                quiet += CHECK_SEC
                if quiet >= HEARTBEAT_SEC:
                    quiet = 0.0
                    yield ": keep-alive\n\n"

            await asyncio.sleep(CHECK_SEC)
    except asyncio.CancelledError:
        # 화면을 닫거나 새로고침하면 여기로 온다. 정상이다.
        raise
    finally:
        _clients -= 1


HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 역방향 프록시가 버퍼링하면 **밀어 주는 의미가 사라진다.**
    "X-Accel-Buffering": "no",
}
