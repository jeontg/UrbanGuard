"""도메인을 늘렸을 때 **조용히 잘못 도는** 자리들 (2026-08-21 flood/traffic 분리).

## 왜 이 시험이 있나

도메인 목록을 리터럴로 박아 둔 곳이 열 군데 넘게 흩어져 있었다. 그중 둘은
빠뜨려도 **에러가 나지 않고 조용히 잘못 동작**한다 — 그래서 시험으로 못박는다.

1. ``model_probe`` 의 분기가 「crowd 가 아니면 무조건 flood」였다. 교통 모델을
   시험 탐지하면 **물 세그멘테이션으로 돌려** 그럴듯한 숫자를 내놓는다.
2. ``routes_cameras`` 의 캘리브레이션 화이트리스트를 빠뜨리면 새 도메인의
   ROI 저장이 전부 400 이 된다(이쪽은 시끄럽게 깨지지만, 화면을 열어 봐야만
   드러난다).

새 도메인을 추가하는 사람이 이 파일을 보고 무엇을 함께 고쳐야 하는지 알 수
있도록, **enum 을 순회하며** 검사한다 — 도메인이 늘면 시험도 자동으로 늘어난다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import cameras as C
from tot_dashboard.core import model_probe, model_registry, settings
from tot_dashboard.core.roles import DOMAIN_LABELS, DOMAIN_SHORT, Domain

ALL_DOMAINS = [d.value for d in Domain]


@pytest.mark.parametrize("domain", ALL_DOMAINS)
def test_모든_도메인이_라벨을_갖는다(domain):
    """라벨이 없으면 화면에 「분류 미상」으로 뜬다."""
    assert DOMAIN_SHORT.get(domain)
    assert DOMAIN_LABELS.get(Domain(domain))
    assert model_registry.DOMAIN_LABELS.get(domain)


@pytest.mark.parametrize("domain", ALL_DOMAINS)
def test_모든_도메인이_운영모델_설정키를_갖는다(domain):
    """없으면 그 도메인 모델을 화면에서 고를 수 없다."""
    key = settings.MODEL_KEYS.get(domain)
    assert key, f"{domain}: MODEL_KEYS 누락"
    assert key in settings.DEFAULTS, f"{domain}: DEFAULTS 기본값 누락"
    assert settings.MODEL_NOTE_KEYS.get(domain)


def _fake_model(monkeypatch):
    """도메인 검사까지 도달하도록 모델 조회만 통과시킨다.

    ``run()`` 은 모델 → 파일 → 도메인 순으로 검사하므로, 진짜 모델 없이
    도메인 검사를 보려면 앞의 두 관문을 열어 줘야 한다.
    """
    fake = type("M", (), {"key": "fake", "exists": True, "label": "fake",
                          "backend": "", "domain": ""})()
    monkeypatch.setattr(model_registry, "get", lambda k: fake)
    monkeypatch.setattr(model_probe.registry, "get", lambda k: fake)
    return fake


@pytest.mark.parametrize("domain", ALL_DOMAINS)
def test_시험탐지가_도메인을_거부하지_않는다(domain, monkeypatch):
    """★ 화이트리스트 회귀 — 새 도메인을 넣고 여기를 안 고치면 시험 탐지가
    통째로 막힌다. 「알 수 없는 도메인」으로 거부되지 않는 것만 본다(실제
    추론은 스트림이 있어야 하므로 여기서 돌리지 않는다)."""
    _fake_model(monkeypatch)
    try:
        model_probe.run(domain=domain, model_key="fake",
                        target_id="__없는지점__", kind="hls")
    except ValueError as e:
        pytest.fail(f"{domain} 이 도메인 화이트리스트에서 거부됐다: {e}")
    except Exception:
        pass  # 대상이 없어 나는 다른 오류는 이 시험의 관심사가 아니다


def test_시험탐지는_정말_모르는_도메인만_거부한다(monkeypatch):
    _fake_model(monkeypatch)
    with pytest.raises(ValueError, match="알 수 없는 도메인"):
        model_probe.run(domain="없는도메인", model_key="fake",
                        target_id="__없는지점__", kind="hls")


def test_교통_시험탐지가_침수_로직으로_새지_않는다():
    """★★ 가장 위험했던 자리 — 예전 분기(`crowd 가 아니면 flood`)를 그대로
    두고 도메인만 늘렸다면, 교통 모델이 **물 세그멘테이션으로 돌아가** 사람이
    「교통 모델을 검증했다」고 오판한다. 전용 함수가 존재하는지 확인한다."""
    assert hasattr(model_probe, "_probe_traffic")
    assert model_probe._probe_traffic is not model_probe._probe_flood


@pytest.mark.parametrize("domain", ALL_DOMAINS)
def test_ROI가_필요한_도메인은_도형_정의가_있다(domain):
    """ROI_SHAPES 에 없으면 S-80 화면에 ROI 편집 칸이 아예 안 나온다."""
    assert C.ROI_SHAPES.get(domain), f"{domain}: ROI_SHAPES 누락"


@pytest.mark.parametrize("domain", ALL_DOMAINS)
def test_필수도형은_그_도메인의_도형목록에_있다(domain):
    """필수라고 해 놓고 목록에 없으면 사용자가 채울 방법이 없다."""
    required = C.REQUIRED_SHAPE.get(domain)
    if required is None:
        return
    keys = {k for k, *_ in C.ROI_SHAPES[domain]}
    assert required in keys, f"{domain}: 필수 도형 {required} 이 목록에 없다"


def test_엑셀_양식의_예시행이_열_수와_맞는다():
    """★ 조용히 깨지는 종류 — 도메인이 늘었는데 예시 행을 안 고치면 값이
    한 칸씩 밀려 엉뚱한 열에 들어간다. openpyxl 이 없으면 건너뛴다."""
    openpyxl = pytest.importorskip("openpyxl")
    import io

    from tot_dashboard.core import camera_bulk

    # template_excel() 은 완성된 .xlsx **바이트**를 돌려준다.
    wb = openpyxl.load_workbook(io.BytesIO(camera_bulk.template_excel()))
    ws = wb["CCTV등록양식"]
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) >= 2
    header, example = rows[0], rows[1]
    assert len(example) == len(header) == len(camera_bulk.COLUMNS)


@pytest.mark.parametrize("domain", ALL_DOMAINS)
def test_엑셀에_도메인별_두_열이_있다(domain):
    from tot_dashboard.core import camera_bulk

    keys = {k for _t, k, _d in camera_bulk.COLUMNS}
    assert f"use_{domain}" in keys
    assert f"cont_{domain}" in keys
