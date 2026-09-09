"""CCTV 재배포 허브(MediaMTX) 연동 — 2026-08-28 신설.

무엇을 고치는가
    4개 탐지 서비스(침수·교통위험·인파·노면)가 각자 원본 CCTV 서버(부산시
    ITS·서울시 TOPIS/spatic)에 개별 연결한다. 실측 기록(``road/
    live_analyzer.py`` 주석) — 같은 카메라에 **세 번째 연결이 시도되면 CCTV
    서버가 거절한다.** 오픈소스 미디어 서버 MediaMTX를 재배포 허브로 두면
    원본에는 카메라당 연결 1개만 열고, 그 뒤에서 RTSP(탐지 서비스용)·
    WebRTC/WHEP(관제요원 브라우저용)로 재배포할 수 있다.

이 모듈의 역할 — 순수하게 URL 조립과 MediaMTX와의 통신만 담당한다. 설정값
(켜짐 여부·호스트·포트)은 ``core/settings.py``가 가진다(``rainfall_provider.py``
가 설정은 ``settings.py``에 두고 실제 조회 로직만 갖는 것과 같은 분리).

⚠️ **재배포는 부가 기능이지 카메라 등록의 필수 조건이 아니다.** 이 모듈의
Control API 호출 함수는 전부 실패를 삼킨다(``traffic_history.record()``가
이력 기록 실패로 이벤트 생성을 막지 않는 것과 같은 원칙) — MediaMTX가
꺼져 있거나 응답이 없어도 카메라 CRUD·서비스 기동 자체가 막히면 안 된다.

⚠️ **폴백 없음(확정 정책, 2026-08-28).** 재배포 서버가 응답하지 않으면
탐지 서비스는 원본 CCTV로 자동 전환하지 않고 "관측 없음"으로 정직하게
실패 표시한다 — 이 모듈은 그 판단에 필요한 헬스체크만 제공하고, 폴백
여부 자체는 호출부(``service/runner.py`` 등)가 결정한다.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from sqlalchemy.orm import Session

from . import settings as ug_settings

log = logging.getLogger("urbanguard.restream")

# Control API 호출 타임아웃. 화면 폴링·서비스 기동 경로에서 쓰이므로,
# 죽은 서버가 응답을 오래 붙잡으면 안 된다(stream_guard.host_reachable과
# 같은 이유).
_API_TIMEOUT_SEC = 3.0

# 헬스체크 결과 캐시. 매 틱(0.2초 안팎)마다 MediaMTX에 HTTP 요청을 보내면
# 자원 낭비다 — stream_guard.DNS_CACHE_SEC(20초)보다 짧게 잡은 이유는
# MediaMTX 프로세스 상태(재시작·경로 과부하)가 DNS보다 훨씬 자주 바뀔 수
# 있기 때문이다.
_HEALTH_TTL_SEC = 10.0

_health_lock = threading.Lock()
_health_cache: tuple[float, bool] | None = None  # (검사 시각, 성공 여부)

# 같은 원본 서버로 짧은 시간에 여러 연결을 한꺼번에 열면 콜드 스타트
# 경합으로 일부가 응답 없이 막힌다 — 2026-08-28 실기 확인: strm4.spatic.
# go.kr 14곳을 한꺼번에 등록했더니 5~6곳이 몇 분째 0바이트로 멈췄고, 같은
# 카메라를 8초 간격으로 순차 등록하니 14곳 전부 정상 연결됐다(단독
# 기동에서도 재현되는 BLOCK-BUSANSTN·BLOCK-CHORYANG 같은 "진짜 호환 안 됨"
# 과는 다른 현상 — 이건 동시 접속 수 문제다). 같은 호스트로 가는 등록
# 사이에만 최소 간격을 둔다 — 서로 다른 호스트는 동시에 등록해도 된다.
_SAME_HOST_STAGGER_SEC = 3.0


def clear_health_cache() -> None:
    """시험용 — 헬스체크 캐시를 비운다."""
    global _health_cache
    with _health_lock:
        _health_cache = None


def _api_base(db: Session | None = None) -> str:
    host = ug_settings.restream_host(db)
    port = ug_settings.restream_api_port(db)
    return f"http://{host}:{port}"


def rtsp_url(camera_id: str, db: Session | None = None) -> str:
    """탐지 서비스(같은 서버 프로세스)가 소비할 RTSP 주소.

    카메라 ID는 이미 고유·ASCII다(``SEOUL-1042`` 등) — MediaMTX 경로
    이름으로 그대로 쓸 수 있다.
    """
    host = ug_settings.restream_host(db)
    port = ug_settings.restream_rtsp_port(db)
    return f"rtsp://{host}:{port}/{quote(camera_id, safe='')}"


def whep_url(camera_id: str, db: Session | None = None,
            request_host: str | None = None) -> str:
    """관제요원 브라우저가 WHEP으로 붙을 주소.

    ``request_host`` — 관리자 설정(``restream.public_host``)이 비어 있을 때
    쓸 호출부의 요청 접속 주소(예: ``request.url.hostname``). 둘 다 없으면
    ``restream_host()``(기본 127.0.0.1)로 물러난다 — 서버 자신에서 열어 본
    관리자에게는 맞지만, 원격 브라우저에서는 실패할 수 있다는 점을 화면에
    안내해야 한다.
    """
    host = (ug_settings.restream_public_host(db) or request_host
            or ug_settings.restream_host(db))
    port = ug_settings.restream_whep_port(db)
    return f"http://{host}:{port}/{quote(camera_id, safe='')}/whep"


def resolved_whep_url(camera_id: str, db: Session | None = None,
                      request_host: str | None = None) -> str | None:
    """관제요원 브라우저가 WHEP으로 재생할 주소 — "재배포 가능한가"부터
    판단한다. 재배포가 꺼져 있거나, 이 카메라가 제외 목록에 있거나,
    MediaMTX가 응답하지 않으면 ``None`` — 호출부(``app.js::openLive()``)가
    hls.js 경로로 자연히 떨어지게 한다.

    ⚠️ 2026-08-28~29 실사용 중 이 3단 판단(enabled→is_excluded→
    mediamtx_healthy→whep_url)이 ``service/runner.py``와
    ``service/main.py``에 각각 따로 구현돼 있다가 **같은 버그(제외
    목록 미확인)가 두 곳에서 따로 발견·수정된 전례**가 있다
    (``tests/service/test_restream_whep_exclusion.py`` 참고). 판단
    로직을 여기 하나로 합쳐 세 번째 재발을 막는다 — 두 호출부는 이제
    이 함수에 위임만 한다.
    """
    try:
        if not ug_settings.restream_enabled(db):
            return None
        if is_excluded(camera_id, db):
            return None
        if not mediamtx_healthy(db):
            return None
        return whep_url(camera_id, db, request_host=request_host)
    except Exception:  # noqa: BLE001
        return None


def rewrite_whep_urls_in_place(blocks: list[dict], request_host: str | None) -> None:
    """R-01(2026-08-29) — 백그라운드 스레드가 캐시해 둔 whep_url을 서빙
    시점의 실제 접속 호스트로 다시 계산해 덮어쓴다.

    ⚠️ 2026-08-31(API 게이트웨이 Phase 4) — 원래 ``service/main.py``에
    ``_rewrite_whep_urls``라는 이름으로 있었는데, 침수·교통위험이
    flood_service.py·traffic_service.py로 갈라지며 **똑같은 로직이
    두 곳에 복제될 뻔했다** — `resolved_whep_url()` 자체가 이미
    "판단 로직이 두 곳에 따로 있다가 같은 버그가 두 번 발견된" 전례
    (위 docstring)로 하나로 합친 함수인데, 그 위에서 이 함수까지
    다시 복제하면 같은 실수를 반복하는 셈이다. 그래서 여기(공유
    코드)로 옮기고 두 서비스가 그대로 임포트해 쓴다.

    ``block_id``·``source_kind``·``whep_url`` 키를 가진 dict 리스트를
    받아 **그 자리에서** 고친다(순수 함수가 아니다 — 호출부가 이미
    "매번 새 dict를 돌려주는 store"를 전제로 쓰고 있어 그 계약에
    맞춘다).
    """
    for b in blocks:
        if b.get("source_kind") == "restream_unavailable":
            # 재배포 서버 자체가 응답이 없어 판정을 아예 안 돌린
            # 상태다 — 호스트를 바꿔도 어차피 재생 불가이므로 그대로
            # None을 유지한다(_snapshot()과 같은 판단).
            b["whep_url"] = None
            continue
        bid = b.get("block_id")
        if bid:
            b["whep_url"] = resolved_whep_url(bid, request_host=request_host)


def mediamtx_healthy(db: Session | None = None) -> bool:
    """MediaMTX가 응답하는가. TTL 안이면 캐시된 값을 그대로 쓴다."""
    global _health_cache
    now = time.monotonic()
    with _health_lock:
        if _health_cache is not None and (now - _health_cache[0]) < _HEALTH_TTL_SEC:
            return _health_cache[1]
    ok = False
    try:
        req = urllib.request.Request(f"{_api_base(db)}/v3/paths/list")
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as r:
            ok = 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        log.debug("MediaMTX 헬스체크 실패: %s", str(e)[:120])
        ok = False
    with _health_lock:
        _health_cache = (now, ok)
    return ok


def _request(method: str, path: str, db: Session | None, body: dict | None = None) -> bool:
    """Control API 호출 공통부. 실패는 전부 삼키고 False만 돌려준다 —
    호출부(카메라 CRUD·기동 동기화)를 절대 막지 않는다."""
    url = f"{_api_base(db)}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SEC) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        # 이미 존재하는 경로에 add를 부르면 400이 올 수 있다 — patch로
        # 갱신을 한 번 더 시도한다(add_path 호출부가 add/patch를 함께 관리).
        log.debug("MediaMTX Control API %s %s 실패: HTTP %s", method, path, e.code)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("MediaMTX Control API 호출 실패(%s %s): %s",
                    method, path, str(e)[:120])
        return False


def is_excluded(camera_id: str, db: Session | None = None) -> bool:
    """이 카메라가 재배포 제외 목록에 있는가.

    ★ 2026-08-28 실기 검증 중 발견 — 부산시 ITS 원본 2곳(Wowza 계열)이
    재생목록 세션을 1회용으로만 허용해 MediaMTX의 표준 HLS 폴링과 근본적
    으로 안 맞는다는 것을 확인했다(디버그 로그로 재현·확정, MediaMTX
    자체의 한계). 이 목록에 오른 카메라는 재배포가 켜져 있어도 원본
    직결을 유지한다 — `to_block_dict()`·`continuous.py::_stream_url()`
    (URL 치환 여부)과 `add_path()`(MediaMTX 경로 등록 여부) 양쪽 모두
    이 함수 하나로 판단해, 두 곳의 판단이 어긋나는 일이 없게 한다.
    """
    return camera_id.strip().upper() in ug_settings.restream_excluded_ids(db)


def is_relay_managed(camera_id: str, db: Session | None = None) -> bool:
    """이 카메라가 ffmpeg 릴레이 대상인가(``restream.relay_ids`` 화이트
    리스트). ``is_excluded()``와 대칭 구조 — 저 목록은 "재배포 자체를
    안 함", 이 목록은 "재배포는 하되 MediaMTX 대신 ffmpeg가 원본을
    읽어 발행하게 함"이다. 2026-08-29 — MediaMTX 자신의 HLS 디먹서가
    간헐적으로 영상을 손상시키는 카메라를 여기 올린다(core/
    ffmpeg_relay.py 모듈 docstring 참고)."""
    return camera_id.strip().upper() in ug_settings.restream_relay_ids(db)


def add_publish_path(camera_id: str, db: Session | None = None) -> bool:
    """MediaMTX에 "발행자를 기다리는" 경로를 등록한다(source 없음) —
    ffmpeg 릴레이가 이 경로로 RTSP 발행(publish)하면 그때부터 살아난다.
    풀(pull) 방식인 ``add_path()``와 반대로, 이 경로는 MediaMTX가
    원본을 직접 안 읽는다 — 그래서 R-03의 ``sourceOnDemand`` 계열
    필드도 여기엔 안 넣는다(그 필드는 풀 경로 전용).

    실기로 확인한 스키마(2026-08-29): ``{"source": "publisher"}``를
    보내면 ``ready:false``인 발행 대기 경로가 만들어지고, 이후 ffmpeg가
    같은 이름으로 RTSP 발행하면 ``ready:true``·``source.type:
    "rtspSession"``으로 바뀐다(직접 curl+ffmpeg로 검증 완료)."""
    name = quote(camera_id, safe="")
    ok = _request("POST", f"/v3/config/paths/add/{name}", db, {"source": "publisher"})
    if not ok:
        ok = _request("PATCH", f"/v3/config/paths/patch/{name}", db, {"source": "publisher"})
    return ok


def add_path(camera_id: str, source_url: str, db: Session | None = None) -> bool:
    """MediaMTX에 카메라 경로를 추가한다. 재배포가 꺼져 있거나 이 카메라가
    제외 목록에 있으면 아무 것도 하지 않는다(설정값 하나로 전체 기능을
    끌 수 있어야 한다는 원칙과 같은 이유로, 제외 카메라는 등록 자체를
    안 해 MediaMTX 로그에 의미 없는 재연결 시도가 쌓이지 않게 한다).

    ⚠️ R-03(2026-08-29) — ``restream.on_demand``가 켜져 있으면 리더
    (RTSP 소비자·WHEP 시청자)가 있을 때만 MediaMTX가 원본에 붙게 한다.
    실측: 등록 33개 중 실제 사용은 9개뿐인데 전부 24시간 원본 연결을
    유지해 하루 239GB가 유입됐다. 등록 범위(활성 카메라 전부)는 그대로
    두고 이 함수 하나만 바꿔, "상시 지정 카메라만 등록"이 깨뜨렸을
    선택 분석 카메라의 실시간 뷰(WHEP)를 건드리지 않는다.

    ⚠️ 같은 날 후속 — ``is_relay_managed()``이면 MediaMTX가 원본을
    직접 안 읽는다. 발행 전용 경로를 만들고(``add_publish_path()``),
    ffmpeg 릴레이를 대신 띄운다(``core/ffmpeg_relay.py::start()``) —
    ffmpeg가 원본을 읽어 MediaMTX에 RTSP로 발행한다."""
    if not ug_settings.restream_enabled(db):
        return False
    if is_excluded(camera_id, db):
        return False
    if is_relay_managed(camera_id, db):
        from . import ffmpeg_relay as _relay  # 지연 임포트(순환 참조 방지)
        ok_path = add_publish_path(camera_id, db)
        ok_relay = _relay.start(camera_id, source_url, rtsp_url(camera_id, db))
        return ok_path and ok_relay
    name = quote(camera_id, safe="")
    body: dict = {"source": source_url}
    if ug_settings.restream_on_demand(db):
        body["sourceOnDemand"] = True
        body["sourceOnDemandStartTimeout"] = (
            f"{ug_settings.restream_on_demand_start_timeout_sec(db)}s")
        body["sourceOnDemandCloseAfter"] = (
            f"{ug_settings.restream_on_demand_close_after_sec(db)}s")
    ok = _request("POST", f"/v3/config/paths/add/{name}", db, body)
    if not ok:
        # 이미 등록된 경로일 수 있다 — 갱신(patch)으로 재시도.
        ok = _request("PATCH", f"/v3/config/paths/patch/{name}", db, body)
    return ok


def remove_path(camera_id: str, db: Session | None = None) -> bool:
    """카메라 삭제 시 MediaMTX 경로도 지운다. 재배포가 꺼져 있어도 시도는
    한다 — 이전에 켜져 있던 동안 등록된 경로가 남아 있을 수 있어서다.

    ffmpeg 릴레이도 항상 끄려고 시도한다 — 이 카메라가 릴레이 대상이
    아니었어도(안 돌고 있어도) ``ffmpeg_relay.stop()``은 no-op이라
    무해하다."""
    from . import ffmpeg_relay as _relay
    _relay.stop(camera_id)
    name = quote(camera_id, safe="")
    return _request("DELETE", f"/v3/config/paths/delete/{name}", db)


def sync_all_paths(db: Session) -> dict:
    """DB의 hls 소스 카메라 전체를 MediaMTX에 채운다. 서비스 기동 시 1회와
    수동 재동기화(관리 화면)에서 쓴다.

    ⚠️ **정본은 DB다** — MediaMTX 설정 YAML을 미리 굽지 않고, 이 함수(와
    카메라 CRUD 훅)가 유일한 "채우는" 경로다. 코드 경로를 하나로 유지해
    기동 시와 CRUD 시가 어긋나지 않게 한다.

    ⚠️ 같은 원본 서버(호스트)로 가는 등록은 ``_SAME_HOST_STAGGER_SEC`` 간격을
    두고 순차로 보낸다 — 카메라 수가 많은 호스트 하나에 몰아치면 전부
    타임아웃난다(위 상수 주석 참고). 카메라가 많을수록 이 함수는 그만큼
    오래 걸릴 수 있다(예: strm4 14곳 = 최대 약 40초) — 호출부는 동기 블로킹
    으로 부르지 말고 별도 스레드에서 실행할 것.
    """
    from . import cameras as C  # 지연 임포트 — 순환 참조 방지

    result = {"total": 0, "ok": 0, "excluded": [], "failed": []}
    if not ug_settings.restream_enabled(db):
        return result
    last_added_at: dict[str, float] = {}  # 호스트별 마지막 등록 시각(단조시계)
    for cam in C.list_all(db, active_only=True):
        if cam.source_type != "hls" or not cam.source_url:
            continue
        # ⚠️ 제외 목록은 실패가 아니라 "의도적으로 안 함"이다 — failed에
        # 섞으면 진짜 문제(원본 연결 실패 등)와 구분이 안 된다.
        if is_excluded(cam.id, db):
            result["excluded"].append(cam.id)
            continue
        host = urlparse(cam.source_url).netloc
        prev = last_added_at.get(host)
        if prev is not None:
            wait = _SAME_HOST_STAGGER_SEC - (time.monotonic() - prev)
            if wait > 0:
                time.sleep(wait)
        last_added_at[host] = time.monotonic()
        result["total"] += 1
        if add_path(cam.id, cam.source_url, db):
            result["ok"] += 1
        else:
            result["failed"].append(cam.id)
    if result["failed"]:
        log.warning("MediaMTX 경로 동기화 — %d개 실패: %s",
                    len(result["failed"]), ", ".join(result["failed"][:10]))
    return result
