"""마이그레이션은 앱 패키지 없이 돌아야 한다 (2026-08-22 전수점검).

## 왜 이 시험이 있나

두 마이그레이션(``b3f7d21ce940``·``c8e19a45b072``)이 시드값을 얻으려고
``tot_dashboard.core.vocabulary`` 를 **런타임에 임포트**하고 있었다. 마이그
레이션은 **그 시점의 스키마를 재현하는 기록**인데 실행 시점의 코드를 끌어다
쓰면 두 가지가 깨진다.

  ① 신규 설치에서 ``alembic upgrade`` 가 앱 패키지 임포트에 의존한다 —
     ``vocabulary.py`` 가 새 모듈을 임포트하게 되는 순간 마이그레이션이
     통째로 실패한다(DB 마이그레이션이 앱 코드보다 먼저 도는 배포에서 특히).
  ② 나중에 어휘가 늘면(2026-08-21 도메인 분리에서 traffic 유형이 실제로
     추가됐다) **과거 마이그레이션이 과거에 없던 값을 심는다.**

``b3e5f1a72c04`` 는 이미 "값을 하드코딩해 패키지 임포트 없이 돌게 한다"는
원칙을 쓰고 있었다 — 이 시험은 그 원칙이 전체에 지켜지는지 못박는다.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


class _BlockAppImports:
    """마이그레이션이 앱 패키지를 임포트하면 곧바로 터뜨린다."""

    def find_module(self, name, path=None):  # noqa: D102 (구식 훅 API)
        if name == "tot_dashboard" or name.startswith("tot_dashboard."):
            raise ImportError(f"마이그레이션이 앱 패키지를 임포트했다: {name}")
        return None


def _migration_files() -> list[Path]:
    return sorted(p for p in VERSIONS.glob("*.py") if not p.name.startswith("__"))


def test_마이그레이션_파일이_실제로_있다():
    """경로가 틀려 0개를 훑고 「전부 통과」라고 말하면 안 된다."""
    assert len(_migration_files()) >= 10


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.stem)
def test_앱_패키지_없이_로드된다(path: Path):
    blocker = _BlockAppImports()
    sys.meta_path.insert(0, blocker)
    try:
        spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.meta_path.remove(blocker)


def test_소스에_앱_패키지_임포트_문자열이_없다():
    """위 시험은 조건부 임포트(함수 안 import)를 놓칠 수 있어 문자열로도 본다."""
    offenders = [p.name for p in _migration_files()
                 if "tot_dashboard" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"앱 패키지를 참조하는 마이그레이션: {offenders}"
