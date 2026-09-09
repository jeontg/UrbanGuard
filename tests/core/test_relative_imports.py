"""상대 임포트가 **패키지 밖으로 나가지 않는가** (2026-08-20 전체 점검).

## 왜 이 시험이 있나

`traffic_weather/perception/traffic_tracker.py` 가 ``from ....common import
tracking`` 이라고 적고 있었다. **점이 하나 많았다.** 거기는 깊이 3이라
`...` 이어야 한다.

그 결과 **침수 블록 루프가 매 틱마다 통째로 실패**했다 — perception·위험도·
스냅샷이 **하나도 갱신되지 않았다.** 로그에 같은 오류가 **54,515회** 쌓여
있었다.

## ⚠️ 왜 아무도 몰랐나

1. 오류가 **함수 안 지연 임포트**라 모듈 임포트 시점에는 안 터진다 —
   시험이 `import` 만으로는 못 잡는다
2. 블록 루프가 예외를 **한 줄로 잘라 삼켰다**. 트레이스백이 없어 5만 번
   나도 **어디서 났는지 알 수 없었다**

★ 그래서 **글자만 보고 잡는다.** 실행하지 않아도, 지연 임포트라도 잡힌다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"


def _offenders() -> list[str]:
    bad = []
    for f in sorted(ROOT.rglob("*.py")):
        rel = f.relative_to(ROOT.parent)          # tot_dashboard/...
        depth = len(rel.parts) - 1                # 파일을 뺀 패키지 깊이
        for n, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = re.match(r"\s*from (\.+)", line)
            if not m:
                continue
            if len(m.group(1)) > depth:
                bad.append(f"{rel}:{n} 점 {len(m.group(1))}개 > 깊이 {depth} · "
                           f"{line.strip()[:70]}")
    return bad


def test_상대_임포트가_패키지_밖으로_나가지_않는다():
    bad = _offenders()
    assert not bad, (
        "패키지 밖을 가리키는 상대 임포트가 있습니다. 실행 중에 "
        "ImportError 로 터집니다:\n" + "\n".join(bad))


def test_검사_대상을_실제로_찾았다():
    """★ 파일을 못 찾으면 위 시험이 **아무것도 안 하고 통과**한다."""
    assert len(list(ROOT.rglob("*.py"))) >= 100
