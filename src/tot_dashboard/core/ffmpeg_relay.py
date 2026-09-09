"""ffmpeg 릴레이 — MediaMTX 자신의 HLS 디먹서가 손상시키는 카메라를
ffmpeg가 대신 읽어 MediaMTX에 RTSP로 발행(publish)한다.

## 왜 필요한가 (2026-08-29, 같은 날 후속)

R-01~03(WHEP 호스트 고정·MediaMTX 무인증 개방·24시간 상시 재배포)을
배포한 뒤, 사용자가 실시간 CCTV 화면이 깨진다고 제보했다. 조사 결과
**MediaMTX 자신의 HLS 디먹서가 원본을 읽는 과정에서 간헐적으로 영상을
손상시킨다**는 것을 실측으로 확인했다 — 같은 순간 원본에 OpenCV(ffmpeg
내장)로 직접 연결하면 깨끗한데, MediaMTX가 재배포한 프레임만 H264
매크로블록 디코딩 실패로 세로줄이 뭉개졌다. mediamtx.log에 "initial
delimiter not found" 경고가 재배포 중인 거의 모든 카메라(부산·서울
원본 둘 다)에서 나타났고(카메라마다 빈도는 1회~25,000회 이상), 같은
증상을 보고한 MediaMTX 공식 GitHub 이슈(#3088)를 확인했다 — 최신
버전(v1.20.1, 이미 최신)까지 관련 수정이 없다.

## 해결 방식 — 왜 ffmpeg 앞단인가

원본을 읽는 주체를 MediaMTX의 (버그 있는) HLS 디먹서에서, 이미 깨끗하게
읽는 것으로 실측 확인된 ffmpeg로 바꾼다. ffmpeg가 ``-c copy``(재인코딩
없는 스트림 카피)로 원본을 읽어 MediaMTX에 RTSP로 발행하면, MediaMTX는
그 결과를 재배포만 하게 돼 손상의 근본 원인(자신의 HLS 디먹서 코드
경로)을 아예 안 탄다.

## ⚠️ 2026-08-30 — 배포 다음날 전부 조용히 죽어 있던 사고

밤새 8개 릴레이가 전부 죽었고 자동복구가 하나도 작동하지 않았다.
조사 결과 원인이 두 겹이었다:

1. **ffmpeg가 죽는 것 자체는 정상적으로 일어난다** — 원본 CCTV
   인코더/스트리밍 서버가 몇 시간에 한 번씩 스스로 스트림 세션을
   리셋한다(재생목록 파일명이 통째로 바뀌며 "Media sequence changed
   unexpectedly" 발생 → 낡은 파일명으로 계속 요청하던 ffmpeg가 404를
   만나 죽음). **이건 막을 수 없다 — 장시간 운영에서 반드시, 주기적으로
   일어난다고 전제해야 한다.**
2. **그런데 죽은 뒤 자동복구가 안 됐다.** 원인을 특정하지 못한 이유
   자체가 문제였다 — 이 모듈이 ``logging`` 모듈을 썼는데, 이 저장소는
   어디에도 ``logging`` 핸들러를 설정해 두지 않아 경고 메시지가
   **전부 허공으로 사라졌다**(실측: ``serve-console.log``를 전수
   검색해도 이 모듈의 로그가 단 한 줄도 없었다). 추가로 ``_spawn()``이
   매 재시작마다 로그 파일 핸들을 열고 한 번도 닫지 않는 누수도
   확인했다. 정확히 몇 번째 재시작 시도에서 무슨 예외로 멈췄는지는
   끝내 특정하지 못했다 — **그래서 "죽으면 재시작"에만 기대지 않고,
   아래처럼 여러 겹으로 다시 설계한다.**

## 재설계 — 왜 이렇게 여러 겹인가

- **로깅을 전부 ``print()``로 바꿨다** — 이 저장소의 다른 실전 로그
  (``[restream]``, ``[runner]`` 등)와 같은 방식. 다음에 또 이런 일이
  생기면 ``serve-console.log``만 봐도 정확히 몇 번째 시도가 무슨
  이유로 실패했는지 바로 보인다.
- **``Popen()`` 직후 부모 쪽 로그 파일 핸들을 닫는다** — 재시작이
  아무리 반복돼도 핸들이 안 쌓인다.
- **감시 루프 안에서 카메라 하나의 예외가 다른 카메라를 막지 못하게
  개별 ``try/except``로 감쌌다** — 카메라 A의 재시작 시도가 계속
  실패해도 B·C·D는 그 틱에서 정상적으로 확인·재시작된다.
- **"죽으면 재시작"보다 한 겹 앞서, 죽기 전에 스스로 주기적으로 새로
  띄운다(선제 재기동)** — 원본이 몇 시간 뒤 세션을 리셋하는 패턴을
  이길 수는 없지만, 그보다 훨씬 짧은 주기로 ffmpeg를 스스로 순환시키면
  매번 마스터 재생목록을 처음부터 새로 읽어 와 "낡은 재생목록 파일명을
  계속 물고 있다가 404를 만나는" 상황 자체가 거의 안 생긴다.
- **``status()``를 실제 엔드포인트에 연결한다**(``service/main.py``
  쪽에서) — "12시간 동안 몰랐다"가 다시는 반복되지 않게, 가동
  대수·누적 재시작 횟수·마지막 재시작 시각을 화면/API로 바로 확인할
  수 있게 한다.

## 적용 범위 — 왜 전체가 아니라 화이트리스트인가

33개 카메라 전부에 상시 적용하면 R-03(``restream.on_demand``)이 없앤
"24시간 상시 원본 연결"(하루 239GB) 문제가 재발할 위험이 있다. 대신
``core/settings.py::restream_relay_ids()`` 화이트리스트에 오른 카메라만
릴레이를 거친다.

## 이 모듈의 역할

순수하게 ffmpeg 서브프로세스 생명주기(시작·종료·감시·재시작)만
담당한다. "이 카메라가 릴레이 대상인가"의 판단은
``core/restream.py::is_relay_managed()``가 하고, 이 모듈은 판단 결과를
받아 프로세스를 다루기만 한다.

⚠️ **재배포는 부가 기능이지 카메라 등록의 필수 조건이 아니다**
(``core/restream.py`` 15~18행과 같은 원칙). 이 모듈의 함수들도 실패를
삼킨다 — ffmpeg가 안 떠도 카메라 CRUD·재배포 자체(pull 방식)는
막히면 안 된다.
"""
from __future__ import annotations

import subprocess
import threading
import time

from ..common.cctv_capture import FFMPEG, USER_AGENT
from ..common.config import PROJECT_ROOT

LOG_DIR = PROJECT_ROOT / "data" / "logs" / "ffmpeg_relay"

# 죽은 프로세스를 얼마나 자주 확인할지. mediamtx_healthy()의 10초 TTL
# 캐시보다 짧게 잡는다 — 릴레이는 AI 판정 입력으로 바로 들어가므로 더
# 민감하게 반응해야 한다.
WATCHDOG_INTERVAL_SEC = 5.0
# 재시작 백오프. 원본이 일시적으로 막혀 있을 때 재시도가 폭주해 원본에
# 부담을 주지 않도록 한다(core/restream.py의 _SAME_HOST_STAGGER_SEC와
# 같은 취지).
BACKOFF_INITIAL_SEC = 5.0
BACKOFF_MAX_SEC = 60.0
# ⚠️ 2026-08-30 — 선제 재기동 주기. 원본이 스스로 스트림 세션을
# 리셋하는 최단 관측 간격이 25분(SEOUL-113, 23:09~23:34)이었다 —
# 그보다 확실히 짧게 잡아, "죽기를 기다렸다 재시작"이 아니라 "죽기
# 전에 스스로 갈아탄다"를 기본으로 삼는다. 매번 마스터 재생목록을
# 처음부터 새로 읽으므로, 원본이 재생목록 파일명을 바꾸는 문제 자체를
# 거의 만나지 않게 된다.
PROACTIVE_RESTART_SEC = 900.0  # 15분

_lock = threading.RLock()
_procs: dict[str, subprocess.Popen] = {}       # camera_id -> Popen
_sources: dict[str, str] = {}                  # camera_id -> 현재 발행 중인 source_url
_publish_urls: dict[str, str] = {}             # camera_id -> 현재 발행 대상 RTSP URL
_started_at: dict[str, float] = {}             # camera_id -> 마지막 기동 시각(monotonic)
_restart_count: dict[str, int] = {}            # camera_id -> 누적 "예기치 않은 죽음" 재시작 횟수
_proactive_count: dict[str, int] = {}          # camera_id -> 누적 선제 재기동 횟수
_next_retry_at: dict[str, float] = {}          # camera_id -> 다음 재시작 시각(monotonic)
_backoff: dict[str, float] = {}                # camera_id -> 현재 백오프(초)
_last_error: dict[str, str] = {}               # camera_id -> 마지막 오류 메시지(요약)
_last_restart_wall: dict[str, str] = {}        # camera_id -> 마지막 재시작 시각(사람이 읽는 문자열)

_watchdog_thread: threading.Thread | None = None


def _stamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ffmpeg_args(source_url: str, publish_url: str) -> list[str]:
    return [
        FFMPEG, "-y", "-loglevel", "warning",
        "-user_agent", USER_AGENT,
        # 원본이 일시적으로 끊기거나(네트워크 지터) HLS 재생목록을 다
        # 읽어(EOF) 새 세그먼트가 나타나기 전까지 잠깐 조용해지는 경우
        # 둘 다 자체적으로 재시도한다 — 실측(2026-08-29): -reconnect_at_eof
        # 없이는 라이브 HLS를 끝까지 다 읽고 나면 "완료"로 보고 종료해
        # 버려, 겉보기엔 멀쩡히 끝났는데 실제로는 릴레이가 죽어 있었다.
        # ⚠️ 원본이 재생목록 파일명 자체를 바꿔버리는 경우(2026-08-30
        # 실측, "Media sequence changed unexpectedly")는 이 플래그들로
        # 못 막는다 — PROACTIVE_RESTART_SEC의 선제 재기동으로 방어한다.
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1", "-reconnect_delay_max", "5",
        "-i", source_url,
        "-c", "copy", "-an",
        "-f", "rtsp", "-rtsp_transport", "tcp",
        publish_url,
    ]


def _log_path(camera_id: str):
    return LOG_DIR / f"{camera_id}.log"


def _spawn(camera_id: str, source_url: str, publish_url: str) -> subprocess.Popen | None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = None
    try:
        log_fh = open(_log_path(camera_id), "a", encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"[ffmpeg_relay] {camera_id}: 릴레이 로그 파일을 열지 못함 — {str(e)[:120]}")
    try:
        proc = subprocess.Popen(
            _ffmpeg_args(source_url, publish_url),
            stdout=(log_fh or subprocess.DEVNULL), stderr=subprocess.STDOUT,
            # Windows: CREATE_NEW_PROCESS_GROUP — terminate()가 자식
            # 콘솔 그룹까지 안정적으로 닫게 한다(training_jobs.py와
            # 같은 패턴). 비-Windows에는 이 상수가 없어 0으로 무해하게.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return proc
    except Exception as e:  # noqa: BLE001
        print(f"[ffmpeg_relay] {camera_id}: ffmpeg 릴레이 기동 실패 — {str(e)[:200]}")
        return None
    finally:
        # ⚠️ 2026-08-30 실측 발견 — Popen()에 넘긴 뒤에도 부모 쪽 핸들을
        # 안 닫으면(자식은 이미 자기 복제본을 가짐) 재시작이 반복될
        # 때마다 파일 핸들이 계속 쌓인다. Popen 성공·실패 어느 쪽이든
        # 부모는 이 핸들이 더 필요 없으므로 항상 닫는다.
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:  # noqa: BLE001
                pass


def start(camera_id: str, source_url: str, publish_url: str) -> bool:
    """이 카메라의 ffmpeg 릴레이를 시작한다(없으면 새로, 있으면
    idempotent). ``source_url``이 이전과 다르면 기존 프로세스를 끄고
    새로 띄운다 — 카메라 정보가 수정된 경우다."""
    with _lock:
        existing = _procs.get(camera_id)
        if existing is not None and existing.poll() is None:
            if _sources.get(camera_id) == source_url:
                return True  # 이미 같은 원본으로 잘 돌고 있다
            print(f"[ffmpeg_relay] {camera_id}: source_url 변경 감지 — 재시작")
            _terminate(existing)
        proc = _spawn(camera_id, source_url, publish_url)
        if proc is None:
            _last_error[camera_id] = "기동 실패"
            return False
        print(f"[ffmpeg_relay] {camera_id}: 릴레이 시작(pid={proc.pid})")
        _procs[camera_id] = proc
        _sources[camera_id] = source_url
        _publish_urls[camera_id] = publish_url
        _started_at[camera_id] = time.monotonic()
        _next_retry_at.pop(camera_id, None)
        _backoff.pop(camera_id, None)
        return True


def stop(camera_id: str) -> None:
    """이 카메라의 릴레이를 끈다. 안 돌고 있어도 무해(no-op) —
    ``remove_path()``가 릴레이 대상 여부와 무관하게 항상 불러도
    안전해야 한다."""
    with _lock:
        proc = _procs.pop(camera_id, None)
        _sources.pop(camera_id, None)
        _publish_urls.pop(camera_id, None)
        _started_at.pop(camera_id, None)
        _next_retry_at.pop(camera_id, None)
        _backoff.pop(camera_id, None)
        if proc is not None:
            _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:  # noqa: BLE001
        pass


def is_running(camera_id: str) -> bool:
    with _lock:
        proc = _procs.get(camera_id)
        return proc is not None and proc.poll() is None


def status() -> dict:
    """/api/health 노출용 — 요청값이 아니라 실제로 돌고 있는 상태를
    보고한다(water_backend_status()와 같은 원칙). 2026-08-30 사고
    이후 신설 — "몇 시간째 몰랐다"가 재발하지 않도록 가동 현황을
    한눈에 확인할 수 있게 한다."""
    with _lock:
        now = time.monotonic()
        running = [cid for cid, p in _procs.items() if p.poll() is None]
        uptimes = {cid: round(now - _started_at[cid]) for cid in running if cid in _started_at}
        return {
            "running": sorted(running),
            "running_count": len(running),
            "uptime_sec": uptimes,
            "unexpected_restart_counts": dict(_restart_count),
            "proactive_restart_counts": dict(_proactive_count),
            "last_restart_at": dict(_last_restart_wall),
            "last_errors": dict(_last_error),
        }


def _restart_one(camera_id: str, proc: subprocess.Popen, now: float, *, proactive: bool) -> None:
    """카메라 하나를 재시작한다. 호출부(``_watchdog_tick``)가 카메라별로
    이 함수를 개별 ``try/except``로 감싸므로, 여기서 예외가 나도 다른
    카메라 처리를 막지 않는다."""
    if not proactive:
        rc = proc.returncode
        if _next_retry_at.get(camera_id, 0.0) > now:
            return  # 아직 백오프 중
    source_url = _sources.get(camera_id)
    publish_url = _publish_urls.get(camera_id)
    if source_url is None or publish_url is None:
        with _lock:
            _procs.pop(camera_id, None)
        return
    if proactive:
        _proactive_count[camera_id] = _proactive_count.get(camera_id, 0) + 1
        n = _proactive_count[camera_id]
        print(f"[ffmpeg_relay] {camera_id}: 선제 재기동(누적 {n}회) — "
              f"{PROACTIVE_RESTART_SEC/60:.0f}분마다 마스터 재생목록을 새로 읽어 "
              "원본의 재생목록 파일명 변경에 미리 대비한다")
        _terminate(proc)
    else:
        _restart_count[camera_id] = _restart_count.get(camera_id, 0) + 1
        _last_error[camera_id] = f"ffmpeg 종료(code={rc})"
        n = _restart_count[camera_id]
        backoff = min(_backoff.get(camera_id, BACKOFF_INITIAL_SEC), BACKOFF_MAX_SEC)
        with _lock:
            _next_retry_at[camera_id] = now + backoff
            _backoff[camera_id] = min(backoff * 2, BACKOFF_MAX_SEC)
        print(f"[ffmpeg_relay] {camera_id}: 예기치 않게 종료됨(code={rc}) — "
              f"재시작 시도(누적 {n}회, 다음 실패 시 백오프 {backoff:.0f}초)")
    new_proc = _spawn(camera_id, source_url, publish_url)
    with _lock:
        if new_proc is not None:
            _procs[camera_id] = new_proc
            _started_at[camera_id] = now
            _last_restart_wall[camera_id] = _stamp()
        # new_proc이 None(기동 실패)이면 _procs[camera_id]는 예전 죽은
        # proc을 그대로 들고 있는다 — 다음 틱에서 poll()이 여전히
        # not None이므로 다시 재시도 대상이 된다(백오프 적용).


def _watchdog_tick() -> None:
    now = time.monotonic()
    with _lock:
        items = list(_procs.items())
    for camera_id, proc in items:
        try:
            if proc.poll() is None:
                # 살아 있다 — 선제 재기동 주기를 넘겼는지만 확인한다.
                started = _started_at.get(camera_id, now)
                if now - started >= PROACTIVE_RESTART_SEC:
                    _restart_one(camera_id, proc, now, proactive=True)
                continue
            # 죽었다 — stop()이 의도적으로 지운 것이면 이미 _procs에서
            # 빠져 있으므로 여기 온 것은 예기치 않은 종료다.
            _restart_one(camera_id, proc, now, proactive=False)
        except Exception as e:  # noqa: BLE001
            # ⚠️ 2026-08-30 — 카메라 하나의 예외가 다른 카메라의 재시작
            # 판단·시도를 막으면 안 된다(2026-08-30 사고의 유력한
            # 원인 중 하나로 의심되는 지점 — 이전에는 이 for 루프
            # 전체가 한 번의 예외로 그 틱을 통째로 포기했다).
            print(f"[ffmpeg_relay] {camera_id}: 감시 틱 처리 중 오류 — {str(e)[:160]}")


def _watchdog_loop() -> None:
    print("[ffmpeg_relay] 감시 스레드 시작")
    while True:
        try:
            _watchdog_tick()
        except Exception as e:  # noqa: BLE001
            print(f"[ffmpeg_relay] 감시 루프 오류: {str(e)[:160]}")
        time.sleep(WATCHDOG_INTERVAL_SEC)


def start_watchdog() -> None:
    """서비스 기동 시 1회 호출한다(제안 위치: main.py lifespan,
    sync_all_paths() 직후). 이미 떠 있으면 아무 것도 안 한다."""
    global _watchdog_thread
    with _lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop, daemon=True, name="ffmpeg-relay-watchdog")
        _watchdog_thread.start()
