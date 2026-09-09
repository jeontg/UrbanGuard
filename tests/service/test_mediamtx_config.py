# -*- coding: utf-8 -*-
"""MediaMTX 커밋 설정(``configs/mediamtx_base.yml``)이 다시 무방비로
되돌아가지 않는가 — 2026-08-29(R-02) 위험 점검 후속.

## 왜 이 시험이 있나

RTSP(8554)·WebRTC(8889)가 인증 없이 모든 인터페이스에 열려 있고
publish 권한까지 무제한이었다 — 같은 망 누구나 CCTV를 볼 수 있고,
임의 카메라 경로에 가짜 영상을 주입해 AI 판정·관제 화면을 오염시킬
수 있었다(실기 확인: 등록된 카메라 경로에 WHIP publish 시도가 인증
없이 그대로 처리됨).

이 시험은 MediaMTX를 실제로 띄우지 않고 **YAML 파일 내용만** 정적으로
검사한다 — 누군가 실수로(또는 문제 재현을 위해 임시로 되돌렸다가) 이
파일을 예전 상태로 커밋하는 것을 잡아내는 최소한의 안전망이다.
`.tools/mediamtx/mediamtx.yml`(실제 구동 사본)은 `.gitignore` 대상이라
CI가 볼 수 없어 검사 대상에서 제외한다 — 배포 스크립트
(``scripts/ensure-mediamtx.ps1``)가 이 커밋된 본을 그대로 복사하므로,
본만 지켜도 새 배포는 안전하다.
"""
from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "mediamtx_base.yml"


def _load() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_설정_파일이_존재한다():
    assert CONFIG_PATH.exists(), f"{CONFIG_PATH} 없음"


def test_RTSP는_loopback에만_바인딩한다():
    """RTSP 소비자는 항상 같은 서버의 우리 앱 프로세스뿐이다
    (core/restream.py::rtsp_url()이 127.0.0.1로만 조립) — 원격에
    열어 둘 이유가 없다."""
    cfg = _load()
    addr = str(cfg.get("rtspAddress", ""))
    assert addr.startswith("127.0.0.1:"), (
        f"rtspAddress가 loopback이 아니다: {addr!r} — 같은 망 누구나 "
        "CCTV RTSP에 접속할 수 있게 된다")


def test_Control_API는_loopback에만_바인딩한다():
    """회귀 방지 — 이미 지켜지던 것이지만 R-02 검사에 나란히 고정한다."""
    cfg = _load()
    addr = str(cfg.get("apiAddress", ""))
    assert addr.startswith("127.0.0.1:"), (
        f"apiAddress가 loopback이 아니다: {addr!r} — 인증 없는 Control "
        "API가 외부에 노출된다")


def test_원격_익명_user에는_publish_권한이_없다():
    """이 시스템의 대부분 카메라 경로는 add_path()가 Control API로 등록한
    source: 풀 방식이라 publish 권한이 필요 없다. 열어 두면 제3자가
    임의 경로에 가짜 영상을 주입할 수 있다(실기로 재현·확인 — 등록된
    경로에 WHIP publish 시도가 인증 없이 처리됨). ``ips: []``(원격
    포함 전체)인 항목에는 publish가 절대 없어야 한다."""
    cfg = _load()
    users = cfg.get("authInternalUsers") or []
    assert users, (
        "authInternalUsers가 비어 있다 — MediaMTX 기본값(무인증, "
        "publish/read/playback 전부 허용)이 조용히 적용된다")
    for u in users:
        if u.get("ips") in (None, []):
            actions = {p.get("action") for p in (u.get("permissions") or [])}
            assert "publish" not in actions, (
                f"원격 접속 가능한 user={u.get('user')!r}(ips={u.get('ips')!r})에 "
                f"publish 권한이 열려 있다: {actions}")


def test_publish_권한은_loopback_전용_항목에만_있다():
    """2026-08-29(같은 날 후속) — MediaMTX 자신의 HLS 디먹서가 일부
    카메라 영상을 간헐적으로 손상시키는 것을 실측으로 확인해, 손상이
    심한 카메라 한정으로 ffmpeg 릴레이(``core/ffmpeg_relay.py``)가
    127.0.0.1에서 RTSP로 발행한다. publish 권한이 존재한다면 반드시
    loopback 전용 user에만 있어야 한다 — 원격 발행(가짜 영상 주입)은
    여전히 막혀 있어야 한다(위 시험과 대칭)."""
    cfg = _load()
    users = cfg.get("authInternalUsers") or []
    for u in users:
        actions = {p.get("action") for p in (u.get("permissions") or [])}
        if "publish" in actions:
            ips = u.get("ips") or []
            assert set(ips) >= {"127.0.0.1", "::1"}, (
                f"publish 권한을 가진 user={u.get('user')!r}의 ips가 "
                f"loopback으로 안 좁혀져 있다: {ips!r}")


def test_원격_읽기_권한은_열려_있다():
    """publish만 막고 read/playback까지 막으면 관제요원의 WHEP 재생이
    깨진다(R-01과 함께 봐야 하는 회귀 지점)."""
    cfg = _load()
    users = cfg.get("authInternalUsers") or []
    all_actions = {p.get("action") for u in users for p in (u.get("permissions") or [])}
    assert "read" in all_actions, "read 권한이 어디에도 없다 — WHEP 재생이 전부 막힌다"
    assert "playback" in all_actions, "playback 권한이 어디에도 없다"


def test_Control_API_권한은_loopback_전용_항목에만_있다():
    """api/metrics/pprof 권한을 준 user 항목의 ips가 loopback으로
    좁혀져 있는지 확인 — 아니면 인증 없는 Control API가 원격에 노출된
    것과 같은 효과다(apiAddress 바인딩과 별개의 방어선)."""
    cfg = _load()
    users = cfg.get("authInternalUsers") or []
    for u in users:
        actions = {p.get("action") for p in (u.get("permissions") or [])}
        if actions & {"api", "metrics", "pprof"}:
            ips = u.get("ips") or []
            assert set(ips) & {"127.0.0.1", "::1"} or ips == ["127.0.0.1", "::1"], (
                f"Control API 권한을 가진 user={u.get('user')!r}의 ips가 "
                f"loopback으로 안 좁혀져 있다: {ips!r}")
