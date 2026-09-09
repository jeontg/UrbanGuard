"""DB 설계서가 **실제 스키마를 따라오고 있는가**.

## 왜 이 시험이 있나 (2026-08-19 발견)

설계서 v7 이후 여러 회차에 걸쳐 표가 늘었는데 문서가 따라오지 못했다.
대조해 보니 **모델 33개 표 중 17개가 설계서에 없었다.** 절반이 넘는다.

⚠️ **납품 산출물이 실제 스키마의 절반만 담고 있었다.** 아무도 몰랐던 이유는
간단하다 — **사람이 눈으로 대조하지 않으면 드러나지 않는 종류**다. 표를
추가할 때 문서를 같이 고치는 것은 잊기 쉽고, 잊어도 아무 일도 안 일어난다.

★ 그래서 **기계가 대조한다.** 표를 새로 만들고 설계서에 안 적으면 여기서
막힌다. 「나중에 문서 정리」가 실제로는 오지 않는다는 것을 이미 겪었다.

## 이 시험이 보지 못하는 것

이름이 적혀 있는지만 본다. **설명이 맞는지는 사람이 봐야 한다.** 그래도
「아예 없는 것」은 확실히 잡는다 — 이번에 문제가 된 것이 그것이었다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tot_dashboard.core.models import Base

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _latest_spec() -> Path:
    """가장 최근 회차의 DB 설계서.

    설계서는 회차 폴더(`docs/<YYYYMMDDHHMM>/`)에 스냅샷으로 쌓인다.
    폴더 이름이 시각이라 이름 정렬이 곧 시간 정렬이다.
    """
    found = sorted(DOCS.glob("*/db_design_spec.md"))
    assert found, "db_design_spec.md 를 찾지 못했습니다."
    return found[-1]


@pytest.fixture(scope="module")
def spec_text() -> str:
    return _latest_spec().read_text(encoding="utf-8")


def test_모든_표가_설계서에_있다(spec_text):
    """★ 표를 새로 만들고 설계서에 안 적으면 여기서 막힌다."""
    missing = [t for t in sorted(Base.metadata.tables)
               if f"`{t}`" not in spec_text]
    assert not missing, (
        f"설계서에 없는 표 {len(missing)}개: {missing}\n"
        f"최신 설계서: {_latest_spec()}\n"
        "납품 산출물이라 실제 스키마와 어긋나면 그대로 낼 수 없습니다.")


def _sections(spec_text: str) -> dict[str, str]:
    """표 이름 → 그 표를 설명하는 절.

    설계서는 ``### 3-16. `risk_levels` — …`` 꼴로 절을 연다. 제목에 적힌
    표 이름으로 절을 가른다.
    """
    out: dict[str, str] = {}
    for blk in re.split(r"^### ", spec_text, flags=re.M)[1:]:
        head = blk.splitlines()[0] if blk.splitlines() else ""
        m = re.search(r"`([a-z_]+)`", head)
        if m:
            out[m.group(1)] = blk
    return out


def test_모든_컬럼이_설계서에_있다(spec_text):
    """⚠️ 표 이름만 맞고 컬럼이 빠지면 더 나쁘다 — 있는 줄 알고 넘어간다.

    ★ **그 표의 절 안에서** 찾는다. 문서 전체에서 이름만 찾으면 **다른 표에
    같은 이름의 컬럼이 있을 때 통과**한다. 실제로 그래서 못 잡았다 —
    `risk_levels.kind` 와 `events.hazard_type_code` 가 빠져 있는데도
    시험은 초록이었다(2026-08-19).
    """
    sections = _sections(spec_text)
    missing = []
    for name in sorted(Base.metadata.tables):
        sec = sections.get(name)
        if sec is None:
            continue          # 표 자체가 없는 것은 위 시험이 잡는다
        for col in Base.metadata.tables[name].columns:
            if f"`{col.name}`" not in sec:
                missing.append(f"{name}.{col.name}")
    assert not missing, f"설계서에 없는 컬럼 {len(missing)}개: {missing[:20]}"


def test_설계서가_마이그레이션_head_를_적고_있다(spec_text):
    """어느 시점 스키마인지 없으면 대조할 기준이 없다."""
    versions = (DOCS.parent / "migrations" / "versions")
    heads = {p.name.split("_")[0] for p in versions.glob("*.py")}
    assert heads, "마이그레이션 파일을 찾지 못했습니다."
    written = {h for h in heads if h in spec_text}
    assert written, (
        "설계서에 마이그레이션 revision 이 하나도 안 적혀 있습니다.")


def test_표_개수를_본문에_적고_있다(spec_text):
    """★ 숫자를 적어 두면 다음 사람이 「몇 개인지」를 눈으로 확인한다."""
    n = len(Base.metadata.tables)
    assert re.search(rf"\b{n}\s*개", spec_text), (
        f"설계서 어디에도 표 개수({n}개)가 적혀 있지 않습니다.")
