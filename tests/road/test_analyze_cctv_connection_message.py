# -*- coding: utf-8 -*-
"""``RoadDefectAnalyzer.analyze_cctv()``의 연결 실패 진단 메시지 —
2026-08-29, 실사용 화면(노면 현황 S-40)에서 신고받은 현상.

## 신고받은 증상 · 원인

노면 상시 순회 화면에 카메라 여러 곳이 "분석 실패 — 연결이 거부되었습니다.
같은 카메라를 다른 탐지서비스가 상시로 보고 있으면 동시 접속이 막힐 수
있습니다"로 계속 떴다. 실제로 확인해 보니:

1. **진짜 원인은 재배포 서버(MediaMTX)가 죽어 있던 것**이었다 —
   재부팅 후 자동으로 안 살아나는 결함(``scripts/urbanguard-run.cmd``에서
   별도로 고침). 재배포를 쓰는 4개 도메인(침수·교통위험·인파·노면) **전부**
   같은 원인으로 영향을 받고 있었다(침수·교통위험은 "관측 없음"으로,
   노면만 이 메시지로 각자 다르게 드러났을 뿐).
2. 그런데 메시지는 "동시접속 경쟁"만 언급해, 재배포 도입 **이전**
   문제(같은 카메라에 다른 탐지서비스가 직접 붙어 경쟁하는 것)를 그대로
   지목하고 있었다 — 재배포가 그 문제를 해결한 뒤로는 더 이상 맞는
   설명이 아니다. 운영자가 엉뚱한 곳(다른 탐지서비스)을 의심하게 만든다.

이 시험은 ``block["source"]["origin_url"]``(재배포로 치환됐다는 표식,
``core/cameras.py::to_block_dict()`` 참고) 유무에 따라 진단 메시지가
갈리는지, 그리고 재배포 서버 자체의 상태(``mediamtx_healthy()``)까지
반영하는지 고정한다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.road import live_analyzer as LA
from tot_dashboard.road.live_analyzer import RoadDefectAnalyzer


def _make_analyzer() -> RoadDefectAnalyzer:
    a = object.__new__(RoadDefectAnalyzer)
    a.model_path = "models/road.pt"
    a.conf = 0.25
    return a


class _NeverOpens:
    """cv2.VideoCapture 대역 — 연결 자체가 안 열린다."""

    def __init__(self, *a, **k):
        pass

    def isOpened(self):
        return False

    def read(self):
        return False, None

    def release(self):
        pass


def _block_with_source(origin_url: str | None):
    src = {"type": "hls", "url": "rtsp://127.0.0.1:8554/TEST-ROAD-MSG"}
    if origin_url:
        src["origin_url"] = origin_url
    return {"id": "TEST-ROAD-MSG", "name": "노면메시지시험", "source": src}


def test_재배포_아닌_지점은_기존_동시접속_메시지_그대로다(monkeypatch):
    """대조군 — 재배포로 치환되지 않은(origin_url 없는) 지점은 예전 메시지를
    그대로 써야 한다(회귀 방지)."""
    monkeypatch.setattr(LA.cv2, "VideoCapture", _NeverOpens)
    monkeypatch.setattr(LA.time, "sleep", lambda s: None)
    analyzer = _make_analyzer()
    block = _block_with_source(origin_url=None)
    res = analyzer.analyze_cctv("TEST-ROAD-MSG", block=block, duration_sec=0.1)
    assert "동시 접속" in res.note
    assert "재배포" not in res.note


def test_재배포_지점에서_재배포_서버가_죽어있으면_그렇게_말한다(monkeypatch):
    monkeypatch.setattr(LA.cv2, "VideoCapture", _NeverOpens)
    monkeypatch.setattr(LA.time, "sleep", lambda s: None)
    monkeypatch.setattr("tot_dashboard.core.restream.mediamtx_healthy", lambda *a, **k: False)
    analyzer = _make_analyzer()
    block = _block_with_source(origin_url="https://example.test/origin.m3u8")
    res = analyzer.analyze_cctv("TEST-ROAD-MSG", block=block, duration_sec=0.1)
    assert "재배포 서버" in res.note
    assert "응답하지 않습니다" in res.note
    assert "동시" not in res.note, "재배포 도입 이전 원인(동시접속)을 잘못 지목했다"


def test_재배포_서버는_살아있는데_이_경로만_안_되면_다르게_말한다(monkeypatch):
    monkeypatch.setattr(LA.cv2, "VideoCapture", _NeverOpens)
    monkeypatch.setattr(LA.time, "sleep", lambda s: None)
    monkeypatch.setattr("tot_dashboard.core.restream.mediamtx_healthy", lambda *a, **k: True)
    analyzer = _make_analyzer()
    block = _block_with_source(origin_url="https://example.test/origin.m3u8")
    res = analyzer.analyze_cctv("TEST-ROAD-MSG", block=block, duration_sec=0.1)
    assert "재배포 서버는 응답하지만" in res.note
    assert "원본 CCTV" in res.note


def test_재배포_상태_조회_자체가_실패해도_분석_결과는_정상_반환된다(monkeypatch):
    """core.restream이 예상 밖으로 예외를 던지는 경우를 흉내낸다 — 진단
    메시지 하나 때문에 분석 결과 자체가 죽으면 안 된다(다른 설정 조회
    실패 처리와 같은 원칙)."""
    monkeypatch.setattr(LA.cv2, "VideoCapture", _NeverOpens)
    monkeypatch.setattr(LA.time, "sleep", lambda s: None)

    def _boom(*a, **k):
        raise RuntimeError("재배포 상태 조회 실패(시험)")

    monkeypatch.setattr("tot_dashboard.core.restream.mediamtx_healthy", _boom)
    analyzer = _make_analyzer()
    block = _block_with_source(origin_url="https://example.test/origin.m3u8")
    res = analyzer.analyze_cctv("TEST-ROAD-MSG", block=block, duration_sec=0.1)
    assert res.frames_analyzed == 0
    assert res.note  # 어떤 문구든 있어야 한다 — 예외로 죽지 않았다는 뜻
