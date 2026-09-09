# -*- coding: utf-8 -*-
"""ffmpeg 릴레이 프로세스 생명주기 — 2026-08-29(같은 날 후속) 신설.

## 왜 이 시험이 있나

MediaMTX 자신의 HLS 디먹서가 일부 카메라 영상을 간헐적으로 손상시키는
것을 실측으로 확인해(원본 직결은 깨끗한데 MediaMTX 재배포만 손상),
화이트리스트에 오른 카메라만 ffmpeg가 원본을 대신 읽어 MediaMTX에
RTSP로 발행하게 한다(``core/ffmpeg_relay.py``). 실제 ffmpeg 프로세스를
띄우지 않고 ``subprocess.Popen``을 가짜로 바꿔 생명주기(시작·idempotent·
원본 변경 감지·종료·감시·재시작·백오프)만 고정한다.
"""
from __future__ import annotations

import subprocess

import pytest

from tot_dashboard.core import ffmpeg_relay as R

CID = "TEST-FFMPEG-RELAY"


class _FakePopen:
    """실제 프로세스 대신 poll()/terminate()만 흉내낸다."""

    _next_pid = 9000

    def __init__(self, *a, **k):
        self.args = a[0] if a else k.get("args")
        _FakePopen._next_pid += 1
        self.pid = _FakePopen._next_pid
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15  # SIGTERM 흉내

    def die(self, code=1):
        """시험에서 "죽었다"를 흉내낼 때 쓴다."""
        self.returncode = code


def _clear_all():
    with R._lock:
        R._procs.clear()
        R._sources.clear()
        R._publish_urls.clear()
        R._started_at.clear()
        R._restart_count.clear()
        R._proactive_count.clear()
        R._next_retry_at.clear()
        R._backoff.clear()
        R._last_error.clear()
        R._last_restart_wall.clear()


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    # 실제 data/logs/ffmpeg_relay를 안 건드리게 임시 디렉터리로 돌린다.
    monkeypatch.setattr(R, "LOG_DIR", tmp_path / "ffmpeg_relay")
    _clear_all()
    yield
    _clear_all()


def test_start는_ffmpeg를_띄운다(monkeypatch):
    created = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: created.append(_FakePopen(*a, **k)) or created[-1])
    ok = R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    assert ok is True
    assert len(created) == 1
    assert R.is_running(CID) is True


def test_start는_같은_source_url이면_idempotent다(monkeypatch):
    created = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: created.append(_FakePopen(*a, **k)) or created[-1])
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    assert len(created) == 1, "같은 source_url인데 다시 띄웠다"


def test_start는_source_url이_바뀌면_재시작한다(monkeypatch):
    created = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: created.append(_FakePopen(*a, **k)) or created[-1])
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    first = created[0]
    R.start(CID, "https://example.test/b.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    assert len(created) == 2, "source_url이 바뀌었는데 재시작 안 했다"
    assert first.terminated is True, "옛 프로세스를 안 끄고 새로 띄웠다"


def test_stop은_안_돌고_있어도_무해하다():
    """no-op 안전성 — remove_path()가 릴레이 대상이 아닌 카메라에도
    항상 stop()을 부르므로, 존재하지 않는 카메라에도 예외가 나면 안
    된다."""
    R.stop("TEST-NEVER-STARTED")  # 예외 없이 통과해야 한다


def test_stop은_실제로_프로세스를_끈다(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    proc = R._procs[CID]
    R.stop(CID)
    assert proc.terminated is True
    assert R.is_running(CID) is False


def test_감시_루프는_죽은_프로세스를_재시작한다(monkeypatch):
    procs = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: procs.append(_FakePopen(*a, **k)) or procs[-1])
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    procs[0].die(1)

    t = [1000.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: t[0])
    R._watchdog_tick()  # 첫 틱에서 바로 재시작(백오프 시각이 과거)

    assert len(procs) == 2, "죽은 프로세스를 재시작하지 않았다"
    assert R.is_running(CID) is True
    assert R._restart_count[CID] == 1


def test_감시_루프는_백오프_시간_전에는_재시작하지_않는다(monkeypatch):
    procs = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: procs.append(_FakePopen(*a, **k)) or procs[-1])
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    procs[0].die(1)

    t = [1000.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: t[0])
    R._watchdog_tick()
    assert len(procs) == 2

    procs[1].die(1)
    t[0] += 1.0  # 백오프(5초) 안
    R._watchdog_tick()
    assert len(procs) == 2, "백오프 시간이 안 지났는데 재시작했다"

    t[0] += 10.0  # 백오프 지남
    R._watchdog_tick()
    assert len(procs) == 3


def test_status는_실제_가동_상태를_반영한다(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    s = R.status()
    assert CID in s["running"]
    assert s["running_count"] == 1

    R._procs[CID].die(1)
    s2 = R.status()
    assert CID not in s2["running"], "죽은 프로세스가 여전히 running으로 보고됐다"


# --- 2026-08-30 사고 후속 — 자동복구가 실제로 끝까지 작동하는지 -------
#
# 배포 다음날 8개 릴레이가 전부 죽은 채 12시간 넘게 방치됐다. 조사 결과
# ① 로깅이 안 보여 원인을 특정 못 했고 ② 파일 핸들 누수가 있었으며
# ③ 감시 루프가 카메라 하나의 예외로 통째로 멈출 수 있는 구조였다.
# 세 가지 모두 회귀로 고정한다.

def test_spawn은_부모_쪽_로그_파일_핸들을_닫는다(monkeypatch, tmp_path):
    """파일 핸들 누수 회귀 방지 — Popen()에 넘긴 뒤에는 부모가 쥔
    핸들을 반드시 닫아야 한다(자식은 이미 자기 복제본을 가진다)."""
    closed = []
    real_open = open

    class _TrackedFile:
        def __init__(self, f):
            self._f = f

        def close(self):
            closed.append(1)
            self._f.close()

        def __getattr__(self, name):
            return getattr(self._f, name)

    def _tracked_open(path, *a, **k):
        return _TrackedFile(real_open(path, *a, **k))

    monkeypatch.setattr("builtins.open", _tracked_open)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    assert closed == [1], "Popen() 이후 부모 쪽 로그 파일 핸들을 안 닫았다"


def test_감시_루프는_한_카메라의_예외가_다른_카메라를_막지_못한다(monkeypatch):
    """2026-08-30 사고의 유력 용의자 — 카메라 A의 재시작 처리에서
    예외가 나도, 나중에 오는 카메라 B는 그 틱에서 정상적으로
    재시작돼야 한다."""
    procs = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: procs.append(_FakePopen(*a, **k)) or procs[-1])
    CID_A, CID_B = "TEST-FFMPEG-RELAY-A", "TEST-FFMPEG-RELAY-B"
    R.start(CID_A, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID_A)
    R.start(CID_B, "https://example.test/b.m3u8", "rtsp://127.0.0.1:8554/" + CID_B)
    for p in procs:
        p.die(1)

    # A만 처리할 때 예외가 나도록 _publish_urls.get()을 A에서만
    # 터지게 바꾼다 — B는 정상 경로 그대로 통과해야 한다.
    class _BoomingDict(dict):
        def get(self, key, default=None):
            if key == CID_A:
                raise RuntimeError("의도적 오류(시험)")
            return super().get(key, default)

    monkeypatch.setattr(R, "_publish_urls", _BoomingDict(R._publish_urls))

    t = [1000.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: t[0])
    R._watchdog_tick()

    assert R.is_running(CID_B) is True, "A의 예외 때문에 B까지 재시작이 안 됐다"


def test_선제_재기동_주기가_지나면_살아있어도_교체한다(monkeypatch):
    """원본이 스스로 재생목록 세션을 리셋하는 것을 막을 수는 없지만,
    그보다 먼저 우리가 스스로 새로 띄우면 마스터 재생목록을 매번
    새로 읽어 그 문제를 거의 만나지 않는다."""
    monkeypatch.setattr(R, "PROACTIVE_RESTART_SEC", 100.0)
    procs = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: procs.append(_FakePopen(*a, **k)) or procs[-1])

    t = [1000.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: t[0])
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    first = procs[0]
    assert first.poll() is None, "죽이지 않았는데 벌써 죽어 있다"

    t[0] += 50.0  # 주기 안
    R._watchdog_tick()
    assert len(procs) == 1, "주기가 안 지났는데 선제 재기동했다"

    t[0] += 60.0  # 주기(100초) 지남
    R._watchdog_tick()
    assert len(procs) == 2, "주기가 지났는데 선제 재기동을 안 했다"
    assert first.terminated is True, "옛 프로세스를 안 끄고 새로 띄웠다"
    assert R._proactive_count[CID] == 1


def test_status에_선제_재기동_횟수와_마지막_재시작_시각이_나온다(monkeypatch):
    monkeypatch.setattr(R, "PROACTIVE_RESTART_SEC", 100.0)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakePopen(*a, **k))
    t = [1000.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: t[0])
    R.start(CID, "https://example.test/a.m3u8", "rtsp://127.0.0.1:8554/" + CID)
    t[0] += 200.0
    R._watchdog_tick()
    s = R.status()
    assert s["proactive_restart_counts"].get(CID) == 1
    assert CID in s["last_restart_at"]
