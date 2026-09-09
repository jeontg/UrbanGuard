"""강수(rainfall) 데이터 출처 선택 — 카메라별 설정 vs 전역 기본값 (2026-08-27).

**배경**: 교통위험 실시간 관제의 차량 검출은 이미 실제 CCTV(YOLO)로 도는데,
강수량만 39개소 전부 합성(sine)값이었다. 카메라마다 JSON을 일일이 고치지
않고도 관리자가 화면 설정(S-95) 하나로 실측(KMA)/합성을 바꿀 수 있도록
``build_rainfall()``에 전역 기본값(``default_type``) 인자를 추가했다.

지켜야 할 것.

* **카메라별 명시 설정이 항상 이긴다** — 전역 기본값은 카메라가 아무것도
  지정하지 않았을 때만 쓰인다
* **키가 없으면 kma를 골라도 sine으로 조용히 되돌아간다** — 서비스가
  멈추는 것보다 합성값으로라도 도는 편이 안전하다는 이 프로젝트의
  일관된 설계(fail-open)
* **기존 호출부(``default_type`` 생략)는 예전과 100% 동일하게 동작한다**
"""
from __future__ import annotations

import pytest

from tot_dashboard.traffic_weather.perception.rainfall_provider import (
    KmaRainfallProvider, MockRainfallProvider, SineRainfallProvider,
    build_rainfall)

COORDS = {"lat": 35.1, "lng": 129.0}


@pytest.fixture
def sine():
    return SineRainfallProvider(peak_mm_h=1.0, period_sec=10.0)


def test_설정이_없으면_기존처럼_sine이다(sine):
    """``default_type``을 생략한 기존 호출부는 예전과 동일해야 한다."""
    p = build_rainfall(None, COORDS, fallback=sine)
    assert p is sine


def test_카메라_설정이_없어도_전역_기본값이_sine이면_그대로_sine이다(sine):
    p = build_rainfall(None, COORDS, fallback=sine, default_type="sine")
    assert p is sine


def test_카메라_설정이_없으면_전역_기본값_kma가_적용된다(monkeypatch, sine):
    """★ 회귀 방지 핵심 — 39개소 전부가 이 경로를 탄다."""
    monkeypatch.setenv("KMA_SERVICE_KEY", "dummy-key")
    p = build_rainfall(None, COORDS, fallback=sine, default_type="kma")
    assert isinstance(p, KmaRainfallProvider)


def test_카메라가_명시한_설정이_전역_기본값보다_우선한다(sine):
    """카메라가 명시적으로 sine을 지정했으면, 전역 기본값이 kma여도 그
    카메라는 sine을 쓴다 — 관리자가 특정 지점만 다르게 두고 싶을 때
    전역 스위치가 그 결정을 덮어써서는 안 된다."""
    p = build_rainfall({"type": "sine"}, COORDS, fallback=sine,
                       default_type="kma")
    assert p is sine


def test_카메라가_명시한_mock_설정도_전역_기본값보다_우선한다(sine):
    p = build_rainfall({"type": "mock", "peak": 10.0}, COORDS,
                       fallback=sine, default_type="kma")
    assert isinstance(p, MockRainfallProvider)


def test_전역_기본값이_kma여도_서비스키가_없으면_sine으로_되돌아간다(
        monkeypatch, sine):
    """fail-open — 키를 안 넣고 실수로 「기상청 실측」을 골라도 탐지
    파이프라인이 멈추면 안 된다."""
    monkeypatch.delenv("KMA_SERVICE_KEY", raising=False)
    p = build_rainfall(None, COORDS, fallback=sine, default_type="kma")
    assert p is sine


def test_모르는_전역_기본값은_sine으로_취급한다(sine):
    """``core.settings.rainfall_backend()``가 이미 목록 밖 값을 걸러내지만,
    이 함수 자체도 방어적으로 sine 취급해야 한다."""
    p = build_rainfall(None, COORDS, fallback=sine, default_type="weather")
    assert p is sine


def test_KMA_조회_실패해도_TTL이_지나기_전엔_재시도하지_않는다(monkeypatch):
    """★ 실기 발견(2026-09-01) — platform-shell 로그에 이 실패 메시지가
    12만 건 넘게 연속으로 찍혀 있었다. 원인은 ``_fetched_at``을 fetch
    "성공" 시에만 갱신하던 것 — KMA가 429(과다 요청)를 계속 돌려주면
    TTL 게이트가 매번 참이 되어, 파이프라인이 매 틱(카메라당 초당 1회)
    쉬지 않고 재시도해 그 자체로 KMA에 대한 요청 폭주(그리고 폭주하는
    urlopen 호출이 쌓이는 시간)를 만든다. 성공/실패와 무관하게 시도
    시각을 먼저 기록해 TTL 동안은 반드시 쉬어야 한다."""
    p = KmaRainfallProvider(35.1, 129.0, "dummy-key", ttl_sec=300.0)
    calls = []

    def _fetch_and_count():
        calls.append(1)
        raise TimeoutError("<urlopen error timed out>")
    monkeypatch.setattr(p, "_fetch", _fetch_and_count)

    p.at(0.0)
    p.at(1.0)
    p.at(2.0)
    assert len(calls) == 1, (
        f"TTL(300초) 안에 재시도 {len(calls)}회 발생 — 폭주 재현됨")
