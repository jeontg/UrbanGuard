"""감시지점(블록) 등록·수정·삭제 (S-80).

지자체가 지점을 하나 늘릴 때마다 우리가 출동해야 하는 구조를 없애는 것이
이 모듈의 목적이다(설계서 5절).

**저장해도 즉시 반영되지 않는다.** 블록 목록은 서비스 기동 시 한 번 읽어
파이프라인 스레드가 붙잡고 있어서, 운영 중에 바꾸면 탐지 스레드와 설정이
어긋난다. 재시작이 필요하다는 사실을 화면에 반드시 알려야 한다.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config_edit import backup

log = logging.getLogger("urbanguard.blocks")

ID_RE = re.compile(r"^[A-Z][A-Z0-9\-_]{2,63}$")
SOURCE_TYPES = {
    "hls": "HLS 스트림 (실시간 CCTV)",
    "video": "동영상 파일",
    "synthetic": "합성 데이터 (시험용)",
}


def config_path() -> Path:
    import os
    from ..common.config import PROJECT_ROOT
    override = os.environ.get("TOT_BLOCKS_PATH")
    return Path(override) if override else PROJECT_ROOT / "configs" / "blocks.json"


def load_raw() -> dict:
    path = config_path()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        log.exception("블록 설정을 읽지 못했습니다: %s", path)
        return {"blocks": []}


def load() -> list[dict]:
    return load_raw().get("blocks", [])


def get(block_id: str) -> dict | None:
    return next((b for b in load() if b.get("id") == block_id), None)


def validate(data: dict, *, existing_id: str | None = None) -> list[str]:
    """입력 검증. 오류 메시지 목록을 돌려준다(비어 있으면 통과).

    지점 하나가 잘못 들어가면 파이프라인 스레드가 기동 중 죽어 **다른 지점의
    탐지까지 멈춘다.** 저장 전에 막는 편이 훨씬 싸다.
    """
    errs: list[str] = []
    bid = (data.get("id") or "").strip()
    if not ID_RE.match(bid):
        errs.append("ID는 대문자로 시작하고 영문 대문자·숫자·하이픈으로 "
                    "3~64자여야 합니다. 예: BLOCK-SEOMYEON")
    if existing_id is None and any(b.get("id") == bid for b in load()):
        errs.append(f"이미 존재하는 ID입니다: {bid}")

    if not (data.get("name") or "").strip():
        errs.append("지점명을 입력하세요.")

    for key, label, lo, hi in (("lat", "위도", 33.0, 39.0),
                               ("lng", "경도", 124.0, 132.0)):
        raw = data.get(key)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            errs.append(f"{label}는 숫자여야 합니다.")
            continue
        # 대한민국 범위를 벗어나면 지도에서 다른 지점들이 한 점으로 뭉친다.
        if not (lo <= v <= hi):
            errs.append(f"{label} 값이 국내 범위({lo}~{hi})를 벗어납니다: {v}")

    stype = data.get("source_type")
    if stype not in SOURCE_TYPES:
        errs.append("영상 소스 종류를 선택하세요.")
    elif stype == "hls":
        url = (data.get("source_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            errs.append("HLS 스트림 주소는 http:// 또는 https:// 로 시작해야 합니다.")
    elif stype == "video":
        if not (data.get("source_path") or "").strip():
            errs.append("동영상 파일 경로를 입력하세요.")
    return errs


def _to_block(data: dict, base: dict | None = None) -> dict:
    """폼 입력을 블록 구조로 바꾼다. 기존 블록의 모르는 필드는 보존한다."""
    out = dict(base or {})
    out["id"] = (data.get("id") or "").strip()
    out["name"] = (data.get("name") or "").strip()
    out["dept"] = (data.get("dept") or "").strip()
    out["coordinates"] = {"lat": float(data["lat"]), "lng": float(data["lng"])}

    stype = data["source_type"]
    src = dict(out.get("source") or {})
    src["type"] = stype
    if stype == "hls":
        src["url"] = (data.get("source_url") or "").strip()
        src.pop("path", None)
        if (data.get("cctv_name") or "").strip():
            src["cctv_name"] = data["cctv_name"].strip()
    elif stype == "video":
        src["path"] = (data.get("source_path") or "").strip()
        src.pop("url", None)
    else:
        src.pop("url", None)
        src.pop("path", None)
    out["source"] = src
    return out


def _save(blocks: list[dict]) -> Path | None:
    path = config_path()
    raw = load_raw()
    raw["blocks"] = blocks
    bak = backup(path)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return bak


def create(data: dict) -> tuple[dict | None, list[str], Path | None]:
    errs = validate(data)
    if errs:
        return None, errs, None
    blocks = load()
    block = _to_block(data)
    blocks.append(block)
    bak = _save(blocks)
    log.info("감시지점 추가 id=%s", block["id"])
    return block, [], bak


def update(block_id: str, data: dict) -> tuple[dict | None, list[str], Path | None]:
    blocks = load()
    idx = next((i for i, b in enumerate(blocks) if b.get("id") == block_id), None)
    if idx is None:
        return None, [f"지점을 찾을 수 없습니다: {block_id}"], None
    data = dict(data)
    data["id"] = block_id          # ID 변경은 허용하지 않는다 — ROI 파일·이벤트
                                    # 이력이 ID로 묶여 있어 바꾸면 연결이 끊긴다
    errs = validate(data, existing_id=block_id)
    if errs:
        return None, errs, None
    blocks[idx] = _to_block(data, base=blocks[idx])
    bak = _save(blocks)
    log.info("감시지점 수정 id=%s", block_id)
    return blocks[idx], [], bak


def delete(block_id: str) -> tuple[bool, list[str], Path | None]:
    blocks = load()
    remain = [b for b in blocks if b.get("id") != block_id]
    if len(remain) == len(blocks):
        return False, [f"지점을 찾을 수 없습니다: {block_id}"], None
    if not remain:
        # 지점이 하나도 없으면 파이프라인이 기동하지 못한다.
        return False, ["마지막 남은 지점은 삭제할 수 없습니다."], None
    bak = _save(remain)
    log.info("감시지점 삭제 id=%s", block_id)
    return True, [], bak
