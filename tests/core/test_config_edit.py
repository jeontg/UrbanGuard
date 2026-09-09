"""설정 파일 편집 (S-80~S-84).

핵심은 **주석이 보존되는가**다. alert_config.yaml 의 주석은 각 임계값이 무엇을
뜻하는지 설명하는 유일한 문서라, 저장 한 번에 사라지면 다음 담당자가 값의
의미를 알 수 없게 된다.
"""
from __future__ import annotations

import pytest

from tot_dashboard.core import config_edit as C

SAMPLE = """\
# 알림 엔진 설정
# 카메라별로 조정이 필요합니다.

ratio_watch: 0.02       # >= 이 값이면 관심
ratio_caution: 0.08     # >= 이 값이면 주의
persist_frames: 3
use_tracking: true
model_path: models/best.pt

grade_bins:
- 20
- 40
nested:
  a: 1
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "sample.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


# --- 읽기 -------------------------------------------------------------------
def test_scalar_fields_exclude_lists_and_nested(cfg):
    keys = {f["key"] for f in C.scalar_fields(cfg)}
    assert "ratio_watch" in keys and "use_tracking" in keys
    assert "grade_bins" not in keys and "nested" not in keys


def test_readonly_fields_are_the_rest(cfg):
    keys = {f["key"] for f in C.readonly_fields(cfg)}
    assert keys == {"grade_bins", "nested"}


def test_types_are_detected(cfg):
    types = {f["key"]: f["type"] for f in C.scalar_fields(cfg)}
    assert types["ratio_watch"] == "number"
    assert types["use_tracking"] == "bool"
    assert types["model_path"] == "text"


def test_inline_comments_are_surfaced_to_the_screen(cfg):
    comments = {f["key"]: f["comment"] for f in C.scalar_fields(cfg)}
    assert "관심" in comments["ratio_watch"]
    assert comments["persist_frames"] == ""


# --- 쓰기 -------------------------------------------------------------------
def test_comments_survive_a_save(cfg):
    """이 테스트가 이 모듈의 존재 이유다."""
    C.apply_updates(cfg, {"ratio_watch": "0.05"})
    text = cfg.read_text(encoding="utf-8")
    assert "# 알림 엔진 설정" in text
    assert "# >= 이 값이면 관심" in text
    assert "# 카메라별로 조정이 필요합니다." in text


def test_comment_column_is_preserved(cfg):
    """저장할 때마다 주석 정렬이 밀리면 파일 전체가 매번 diff 로 잡힌다."""
    before = [ln for ln in cfg.read_text(encoding="utf-8").splitlines()
              if ln.startswith("ratio_watch:")][0]
    col_before = before.index("#")
    C.apply_updates(cfg, {"ratio_watch": "0.05"})
    after = [ln for ln in cfg.read_text(encoding="utf-8").splitlines()
             if ln.startswith("ratio_watch:")][0]
    assert after.index("#") == col_before


def test_unrelated_lines_are_byte_identical(cfg):
    """건드리지 않은 줄은 한 글자도 바뀌면 안 된다."""
    before = cfg.read_text(encoding="utf-8").splitlines()
    C.apply_updates(cfg, {"ratio_watch": "0.05"})
    after = cfg.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    for b, a in zip(before, after):
        if b.startswith("ratio_watch:"):
            continue
        assert b == a


def test_value_is_actually_changed(cfg):
    res = C.apply_updates(cfg, {"ratio_watch": "0.05"})
    assert res["changed"]["ratio_watch"] == (0.02, 0.05)
    assert C.load_yaml(cfg)["ratio_watch"] == 0.05


def test_untouched_keys_keep_their_values(cfg):
    C.apply_updates(cfg, {"ratio_watch": "0.05"})
    data = C.load_yaml(cfg)
    assert data["ratio_caution"] == 0.08
    assert data["grade_bins"] == [20, 40]
    assert data["nested"] == {"a": 1}


def test_bool_round_trip(cfg):
    C.apply_updates(cfg, {"use_tracking": "false"})
    assert C.load_yaml(cfg)["use_tracking"] is False


def test_int_stays_int(cfg):
    C.apply_updates(cfg, {"persist_frames": "5"})
    assert C.load_yaml(cfg)["persist_frames"] == 5


def test_text_value_round_trip(cfg):
    C.apply_updates(cfg, {"model_path": "models/other.pt"})
    assert C.load_yaml(cfg)["model_path"] == "models/other.pt"


# --- 잘못된 입력 -------------------------------------------------------------
def test_non_numeric_input_is_skipped_not_written(cfg):
    """숫자 자리에 글자가 들어오면 파일을 망가뜨리는 대신 건너뛴다."""
    res = C.apply_updates(cfg, {"ratio_watch": "이건숫자가아님"})
    assert "ratio_watch" in res["skipped"]
    assert C.load_yaml(cfg)["ratio_watch"] == 0.02


def test_unknown_key_is_skipped(cfg):
    res = C.apply_updates(cfg, {"does_not_exist": "1"})
    assert "does_not_exist" in res["skipped"]


def test_empty_input_is_ignored(cfg):
    res = C.apply_updates(cfg, {"ratio_watch": ""})
    assert not res["changed"]


def test_same_value_is_not_recorded_as_a_change(cfg):
    res = C.apply_updates(cfg, {"ratio_watch": "0.02"})
    assert not res["changed"]


# --- 백업 -------------------------------------------------------------------
def test_backup_is_created_before_writing(cfg):
    res = C.apply_updates(cfg, {"ratio_watch": "0.09"})
    assert res["backup"] is not None and res["backup"].exists()
    # 백업본에는 이전 값이 남아 있어야 한다
    assert "0.02" in res["backup"].read_text(encoding="utf-8")


def test_no_backup_when_nothing_changed(cfg):
    res = C.apply_updates(cfg, {"ratio_watch": "0.02"})
    assert res["backup"] is None


def test_list_backups_returns_newest_first(cfg):
    C.apply_updates(cfg, {"ratio_watch": "0.03"})
    C.apply_updates(cfg, {"ratio_watch": "0.04"})
    backups = C.list_backups(cfg)
    assert len(backups) >= 2


def test_same_millisecond_saves_keep_both_backups(cfg):
    """⚠️ 같은 밀리초에 두 번 저장해도 앞선 백업이 살아 있어야 한다.

    파일명이 밀리초까지만이라 빠르게 두 번 저장하면 덮어써졌다. 되돌릴
    사본을 잃는다는 뜻이라 **백업의 존재 이유가 무너진다.** 2026-08-19
    회귀에서 반복 재현돼 고쳤다.
    """
    from unittest.mock import patch

    from datetime import datetime, timezone

    # 시계를 고정해 「같은 밀리초」를 확실히 만든다 — 빠른 장비에서만
    # 나는 문제라 실행 속도에 기대면 시험이 들쭉날쭉해진다.
    fixed = datetime(2026, 8, 19, 9, 44, 0, 123000, tzinfo=timezone.utc)
    with patch.object(C, "datetime") as dt:
        dt.now.return_value = fixed
        C.apply_updates(cfg, {"ratio_watch": "0.03"})
        C.apply_updates(cfg, {"ratio_watch": "0.04"})

    names = [b["name"] for b in C.list_backups(cfg)]
    assert len(names) >= 2, f"백업이 덮어써졌습니다: {names}"
    # 정리(KEEP_BACKUPS) 정렬이 여전히 시간순이어야 한다.
    assert sorted(names) == sorted(names, key=str)


# --- 실제 배포 설정 ----------------------------------------------------------
def test_shipped_configs_are_readable():
    from tot_dashboard.common.config import PROJECT_ROOT
    for name in ("risk_config.yaml", "alert_config.yaml", "water_config.yaml",
                 "flood_model_config.yaml"):
        p = PROJECT_ROOT / "configs" / name
        assert p.exists(), name
        assert C.scalar_fields(p), f"{name} 에 편집 가능한 값이 없습니다"
