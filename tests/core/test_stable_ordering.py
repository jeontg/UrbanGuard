"""시각으로 정렬할 때 **순서가 확정되는가** (2026-08-20 전체 점검).

## 왜 이 시험이 있나

`crowd_observations` 를 `observed_at` **하나로만** 정렬하고 있었다. 같은 초에
들어온 관측은 서로 순위가 없어(tie), **실행마다 순서가 뒤집힌다.**

실제로 「최근이 먼저」를 검사하는 시험이 4·6·8 중 **4를 먼저** 받아 깨졌다.
전체 스위트에서만 재현되고 파일 단독으로는 안 나서, **순서 의존 결함**으로
숨어 있었다.

## ⚠️ 시험 문제가 아니라 운영 문제다

관제요원이 「가장 최근 관측」이라고 믿는 값이 사실은 **그 초의 아무 것**이
된다. 노면 이력·이벤트 목록·오류 목록도 같은 구조였다.

★ `id` 는 단조 증가하므로 묶인 순서를 확실히 깬다.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "tot_dashboard"

# 「최근 순」이 뜻을 갖는 시각 칸들. 여기로 정렬하면 2차 키가 있어야 한다.
_TIME_COLS = ("observed_at", "analyzed_at", "detected_at", "created_at",
              "last_seen_at")


def _offenders() -> list[str]:
    """시각 칸 하나로만 끝나는 order_by 를 찾는다.

    ⚠️ **글자만 본다.** 여러 줄에 걸친 정렬은 못 잡는다 — 그건 이 시험의
    한계이고, 대신 **한 줄짜리 흔한 실수는 확실히 잡는다.**
    """
    pat = re.compile(
        r"order_by\(\s*[A-Za-z_]+\.(" + "|".join(_TIME_COLS) +
        r")\.(?:desc|asc)\(\)\s*\)")
    bad = []
    for f in sorted(SRC.rglob("*.py")):
        for n, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{f.relative_to(SRC.parent)}:{n} · {line.strip()[:80]}")
    return bad


def test_시각_정렬에_2차_키가_있다():
    bad = _offenders()
    assert not bad, (
        "시각 하나로만 정렬하면 같은 초의 순서가 뒤집힙니다. "
        "`id` 를 2차 키로 넣으십시오:\n" + "\n".join(bad))


def test_검사_대상을_실제로_찾았다():
    """★ 파일을 못 찾으면 위 시험이 **아무것도 안 하고 통과**한다."""
    assert len(list(SRC.rglob("*.py"))) >= 100
