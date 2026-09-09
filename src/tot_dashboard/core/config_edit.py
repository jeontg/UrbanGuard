"""설정 파일 편집 (S-80 ~ S-84).

지자체 담당자가 파일을 직접 고칠 수는 없으므로 화면에서 바꾼다. 다만 저장
방식에 제약이 하나 있다.

**YAML 주석을 보존해야 한다.** `alert_config.yaml` 의 주석은 각 임계값이 무엇을
뜻하는지 설명하는 유일한 문서다. `yaml.safe_dump` 로 다시 쓰면 주석이 전부
사라져 다음 담당자가 값의 의미를 알 수 없게 된다. 그래서 파일을 통째로 다시
쓰지 않고 **해당 줄의 값만 치환**한다.

저장 전에는 항상 백업을 남긴다. 임계값을 잘못 바꾸면 탐지가 통째로 어긋나는데,
되돌릴 방법이 없으면 운영이 멈춘다.
"""
from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger("urbanguard.config_edit")

BACKUP_DIR_NAME = "_backup"
KEEP_BACKUPS = 20

# `key: value  # 주석` 형태의 최상위 스칼라 한 줄
_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w]*)\s*:\s*"
                   r"(?P<value>[^#\n]*?)(?P<gap>\s*)(?P<comment>#.*)?$")


def load_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        log.exception("설정을 읽지 못했습니다: %s", path)
        return {}


def scalar_fields(path: Path) -> list[dict]:
    """화면에서 고칠 수 있는 항목 목록.

    최상위 스칼라(숫자·문자·불리언)만 대상으로 한다. 리스트·중첩 구조는 줄 단위
    치환으로 안전하게 다룰 수 없어 읽기 전용으로 둔다 — 잘못 건드리면 파일이
    깨지고, 그 피해가 편집 편의보다 크다.
    """
    data = load_yaml(path)
    comments = _comments_of(path)
    out = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue
        out.append({
            "key": key,
            "value": value,
            "type": ("bool" if isinstance(value, bool)
                     else "number" if isinstance(value, (int, float))
                     else "text"),
            "comment": comments.get(key, ""),
        })
    return out


def readonly_fields(path: Path) -> list[dict]:
    """리스트·중첩 구조. 화면에는 보여 주되 편집은 막는다."""
    data = load_yaml(path)
    return [{"key": k, "value": v} for k, v in data.items()
            if isinstance(v, (dict, list))]


def _comments_of(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _LINE.match(line)
            if m and m.group("indent") == "" and m.group("comment"):
                out[m.group("key")] = m.group("comment").lstrip("#").strip()
    except Exception:  # noqa: BLE001
        pass
    return out


def _format(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def backup(path: Path) -> Path | None:
    """저장 직전 사본을 남긴다. 실패해도 저장 자체는 막지 않는다."""
    try:
        d = path.parent / BACKUP_DIR_NAME
        d.mkdir(exist_ok=True)
        # 밀리초까지 넣는다 — 같은 초에 두 번 저장하면 앞선 백업이 덮어써진다.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
        dest = d / f"{path.stem}.{stamp}{path.suffix}"
        # ⚠️ **밀리초도 부딪힌다.** 화면에서 빠르게 두 번 저장하면 같은
        # 밀리초에 들어와 앞선 백업을 덮어썼다 — 되돌릴 사본을 잃는다는 뜻이라
        # 백업의 존재 이유가 무너진다. 실제로 회귀에서 반복 재현됐다.
        #
        # 마이크로초로 늘리는 대신 **이름이 겹치면 번호를 붙인다.** 정밀도를
        # 높이는 것은 확률을 낮출 뿐 없애지 못한다.
        if dest.exists():
            for n in range(1, 1000):
                cand = d / f"{path.stem}.{stamp}-{n}{path.suffix}"
                if not cand.exists():
                    dest = cand
                    break
        shutil.copy2(path, dest)
        olds = sorted(d.glob(f"{path.stem}.*{path.suffix}"))
        for old in olds[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        return dest
    except Exception:  # noqa: BLE001
        log.exception("백업 실패: %s", path)
        return None


def apply_updates(path: Path, updates: dict[str, str]) -> dict:
    """값만 치환해 저장한다. 주석·순서·빈 줄은 그대로 둔다.

    반환값: {"changed": {key: (before, after)}, "skipped": [key], "backup": Path|None}
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    current = load_yaml(path)

    changed: dict[str, tuple] = {}
    skipped: list[str] = []
    remaining = dict(updates)

    for i, line in enumerate(lines):
        m = _LINE.match(line.rstrip("\n"))
        if not m or m.group("indent") != "":
            continue
        key = m.group("key")
        if key not in remaining:
            continue
        raw = remaining.pop(key)
        before = current.get(key)
        after = _coerce(raw, before)
        if after is None:
            skipped.append(key)
            continue
        if _format(after) == _format(before):
            continue
        newline = f"{key}: {_format(after)}"
        if m.group("comment"):
            # 주석이 원래 있던 열을 유지한다. 저장할 때마다 정렬이 흐트러지면
            # 파일 전체가 매번 diff 로 잡혀 변경 이력을 읽기 어려워진다.
            col = len(m.group("indent")) + len(key) + 2 + \
                len(m.group("value")) + len(m.group("gap"))
            newline += " " * max(2, col - len(newline)) + m.group("comment")
        lines[i] = newline + ("\n" if line.endswith("\n") else "")
        changed[key] = (before, after)

    skipped += list(remaining)  # 파일에 없는 키
    bak = None
    if changed:
        bak = backup(path)
        path.write_text("".join(lines), encoding="utf-8")
        log.info("설정 저장 %s — 변경 %d건", path.name, len(changed))
    return {"changed": changed, "skipped": skipped, "backup": bak}


def _coerce(raw: str, sample):
    """입력값을 기존 값의 타입으로 바꾼다. 실패하면 None(=건너뜀)."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    if isinstance(sample, bool):
        low = raw.lower()
        if low in ("true", "1", "on", "yes"):
            return True
        if low in ("false", "0", "off", "no"):
            return False
        return None
    if isinstance(sample, int) and not isinstance(sample, bool):
        try:
            return int(float(raw)) if float(raw).is_integer() else float(raw)
        except ValueError:
            return None
    if isinstance(sample, float):
        try:
            return float(raw)
        except ValueError:
            return None
    return raw


def list_backups(path: Path, limit: int = 10) -> list[dict]:
    d = path.parent / BACKUP_DIR_NAME
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob(f"{path.stem}.*{path.suffix}"), reverse=True)[:limit]:
        out.append({"name": p.name,
                    "at": datetime.fromtimestamp(p.stat().st_mtime)})
    return out
