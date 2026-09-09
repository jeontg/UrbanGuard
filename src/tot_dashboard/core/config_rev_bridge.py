"""``config_rev``를 프로세스 경계 너머로 전파한다 — PostgreSQL LISTEN/NOTIFY.

[[config_rev]]는 일부러 순수 인메모리(``threading.Condition``)로 남겨 뒀다 —
DB 없이도 단위 시험이 돌아야 하고, flood/traffic처럼 같은 프로세스 안에서
쓰는 곳은 DB 왕복이 전혀 필요 없기 때문이다. 문제는 API 게이트웨이
Phase 2로 road-service가 별도 프로세스가 되면서 생겼다 — platform-shell이
카메라 설정을 바꿔 ``config_rev.bump()``를 불러도, **다른 프로세스인**
road-service의 ``RoadContinuousWatcher``는 그 신호를 받을 방법이 없어
자기 순회 주기(``ROAD_PERIOD_SEC``, 기본 15분 — 실측 확인:
``/api/health``의 ``continuous.road.period_sec`` = 900)가 끝날 때까지
기다린다. 완전히 끊긴 건 아니지만 "즉시 반영"이 아니게 된다.

이 모듈은 그 틈만 메운다 — ``config_rev`` 자체는 손대지 않는다.
    - 설정을 바꾸는 쪽(platform-shell)은 ``bump()`` 직후 [[notify]]를 불러
      ``NOTIFY urbanguard_config_rev``를 보낸다.
    - 신호를 받아야 하는 쪽(road-service)은 기동 시 [[listen_loop]]를
      백그라운드 스레드로 띄운다 — ``LISTEN``으로 대기하다 알림이 오면
      로컬 ``config_rev.bump()``를 호출해, 그 프로세스 안에서 이미 쓰고
      있는 ``wait_change_or_stop()`` 대기자를 그대로 깨운다.

DB가 잠깐 죽어 있어도 설정 저장 자체(``bump()``)는 항상 성공해야 하므로,
[[notify]] 실패는 경고만 남기고 삼킨다 — 최악의 경우 기존과 같은 「최대
순회 주기만큼 지연」으로 되돌아갈 뿐, 저장 API가 500을 내면 안 된다.
"""
from __future__ import annotations

import threading

import psycopg

from . import config_rev
from .db import database_url

# ⚠️ 이 파일 안의 상태 메시지는 ``logging`` 대신 ``print()``를 쓴다 — 이
# 저장소 로깅 관례(``video_quality.py``·``ffmpeg_relay.py``·
# ``road_service.py``의 lifespan 등)와 맞춘다. 앱 전역에 콘솔/로그 파일로
# 이어지는 핸들러가 붙어 있는 건 ``logging`` 모듈 로거가 아니라 이
# ``print()`` 뿐이다 — ``log.info``로 남기면 아무 데도 보이지 않는다
# (실기 확인: LISTEN 시작 메시지를 ``logging``으로 남겼더니 콘솔 로그에
# 전혀 안 찍혔다).

_CHANNEL = "urbanguard_config_rev"
_RETRY_SEC = 2.0


def _dsn() -> str:
    """SQLAlchemy용 URL(``postgresql+psycopg://...``)을 libpq DSN으로 바꾼다.

    psycopg는 SQLAlchemy 방언 접미사(``+psycopg``)를 모른다 — 이것만
    떼어내면 나머지는 그대로 libpq가 읽을 수 있는 형태다.
    """
    url = database_url()
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def notify() -> None:
    """다른 프로세스의 [[listen_loop]]를 즉시 깨운다(최선 노력, 실패해도 무시).

    ``config_rev.bump()`` 호출 직후 짝으로 부른다. 여기서 예외가 나도
    (DB가 잠깐 끊겼거나 느려도) 설정 저장 자체를 실패시키면 안 되므로
    조용히 경고만 남긴다 — 그 경우 road-service는 기존처럼 자기 순회
    주기만큼의 폴링 지연으로 돌아갈 뿐이다.
    """
    try:
        with psycopg.connect(_dsn(), autocommit=True, connect_timeout=2) as conn:
            conn.execute(f"NOTIFY {_CHANNEL}")
    except Exception as exc:  # noqa: BLE001 - 최선 노력, 절대 전파하지 않는다
        print(f"[config_rev_bridge] NOTIFY 실패(무시하고 계속): {str(exc)[:160]}",
              flush=True)


def listen_loop(stop_event: threading.Event) -> None:
    """``stop_event``가 설정될 때까지 LISTEN하며 로컬 ``config_rev``를 갱신한다.

    다른 프로세스(platform-shell)의 [[notify]]가 보낸 신호를 받아
    ``config_rev.bump()``를 호출 — 이 프로세스 안에서 ``wait_change_or_stop``
    으로 자고 있는 워처가 그대로 즉시 깨어난다. 연결이 끊기면
    ``_RETRY_SEC`` 후 재연결을 시도한다(운영 DB 재기동 등에도 죽지 않게).
    """
    while not stop_event.is_set():
        try:
            with psycopg.connect(_dsn(), autocommit=True) as conn:
                conn.execute(f"LISTEN {_CHANNEL}")
                print(f"[config_rev_bridge] LISTEN 시작(channel={_CHANNEL})",
                      flush=True)
                while not stop_event.is_set():
                    for _note in conn.notifies(timeout=1.0):
                        config_rev.bump()
        except Exception as exc:  # noqa: BLE001 - 재연결 루프이므로 계속 진행
            if stop_event.is_set():
                break
            print(f"[config_rev_bridge] LISTEN 연결 끊김, {_RETRY_SEC:.0f}초 후 "
                  f"재시도: {str(exc)[:160]}", flush=True)
            stop_event.wait(_RETRY_SEC)
