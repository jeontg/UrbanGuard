# -*- coding: utf-8 -*-
"""``_build_source()``의 CCTV 재배포 허브 "폴백 없음" 정책 — 2026-08-28 신설.

## 왜 이 시험이 있나

재배포 서버(MediaMTX)가 켜져 있는 카메라의 연결이 실패하면, 기존의
"원본 CCTV 연결 실패 → synthetic 폴백" 경로를 타면 안 된다(확정 정책 —
원본은 동시접속 한계 때문에 재배포를 도입한 것이라, 조용히 원본 직결로
돌아가면 그 문제가 그대로 재발한다). 대신 ``kind="restream_unavailable"``
로 표시해 ``_block_loop``가 판정을 건너뛰고 "관측 없음"으로 정직하게
남기게 한다.

``to_block_dict()``가 재배포로 치환했을 때만 ``source.origin_url``을
함께 남겨 두므로, ``_build_source()``는 그 값의 존재 여부만으로 "이
target이 재배포 주소인가"를 판단한다(URL 문자열 패턴을 추측하지 않는다).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tot_dashboard.service.runner import PipelineRunner


def _make_runner() -> PipelineRunner:
    r = object.__new__(PipelineRunner)
    r.fps = 5.0
    return r


class _RaisingYolo:
    def __init__(self, *a, **k):
        raise RuntimeError("연결 거부됨(시험)")


def test_재배포_주소_연결_실패는_synthetic이_아니라_restream_unavailable이다(monkeypatch):
    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.detection_source.YoloDetectionSource",
        _RaisingYolo)
    runner = _make_runner()
    block = {"id": "TEST-RESTREAM-NOFB", "source": {
        "type": "hls", "url": "rtsp://127.0.0.1:8554/TEST-RESTREAM-NOFB",
        "origin_url": "https://example.test/original.m3u8",
    }}
    src, baseline, kind = runner._build_source(block, rainfall=SimpleNamespace())
    assert kind == "restream_unavailable", (
        "재배포 연결 실패인데 kind가 'synthetic'으로 떨어졌다 — "
        "원본 CCTV로 조용히 전환하는 것과 같은 효과라 정책 위반이다")
    # 기계적 프레임 생성을 위해 SyntheticDetectionSource를 재사용하지만,
    # ctx 구성이 source.frames()를 즉시 부를 수 있어야 하므로 None이면 안 된다.
    assert src is not None
    assert hasattr(src, "frames")


def test_재배포가_꺼져있으면_기존처럼_synthetic으로_떨어진다(monkeypatch):
    """대조군 — 이번 변경이 기존(재배포 미사용) 동작을 깨면 안 된다."""
    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.detection_source.YoloDetectionSource",
        _RaisingYolo)
    runner = _make_runner()
    block = {"id": "TEST-RESTREAM-NOFB-2", "source": {
        "type": "hls", "url": "https://example.test/original.m3u8",
        # origin_url 없음 — 재배포로 치환되지 않은, 원본 그대로인 상태.
    }}
    src, baseline, kind = runner._build_source(block, rainfall=SimpleNamespace())
    assert kind == "synthetic"


def test_연결이_성공하면_재배포_여부와_무관하게_실제_kind를_돌려준다(monkeypatch):
    class _OkYolo:
        def __init__(self, *a, **k):
            self.frame_wh = (640, 360)

        def frames(self):
            yield (0.0, [], None)

    monkeypatch.setattr(
        "tot_dashboard.traffic_weather.perception.detection_source.YoloDetectionSource",
        _OkYolo)
    runner = _make_runner()
    block = {"id": "TEST-RESTREAM-NOFB-3", "source": {
        "type": "hls", "url": "rtsp://127.0.0.1:8554/TEST-RESTREAM-NOFB-3",
        "origin_url": "https://example.test/original.m3u8",
    }}
    src, baseline, kind = runner._build_source(block, rainfall=SimpleNamespace())
    assert kind == "hls"
    assert isinstance(src, _OkYolo)
