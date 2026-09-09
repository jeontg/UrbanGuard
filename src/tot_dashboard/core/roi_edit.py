"""ROI 설정 저장 (S-81).

ROI가 없으면 물 면적 판정이 부정확해진다. 지금까지는 별도 CLI 도구로만
설정할 수 있어 지점을 늘릴 때마다 우리가 개입해야 했다.

좌표는 **정지영상의 픽셀 좌표**로 저장한다. 화면 크기가 달라도 같은 영역을
가리키도록, 브라우저에서 표시 배율을 되돌려 원본 해상도 기준으로 보낸다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config_edit import backup

log = logging.getLogger("urbanguard.roi")

LAYERS = {
    "road_roi": "도로 ROI",
    "low_point_roi": "저지대 ROI",
    "lane_threshold_line": "침수 경계선",
}


def roi_dir() -> Path:
    import os
    from ..common.config import PROJECT_ROOT
    override = os.environ.get("URBANGUARD_ROI_DIR")
    return Path(override) if override else PROJECT_ROOT / "configs" / "roi"


def roi_path(block_id: str) -> Path:
    return roi_dir() / f"{block_id}.json"


def load(block_id: str) -> dict:
    path = roi_path(block_id)
    if not path.exists():
        return {"camera_name": block_id, "frame_width": 0, "frame_height": 0,
                "road_roi": [], "low_point_roi": [], "lane_threshold_line": []}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        log.exception("ROI 파일을 읽지 못했습니다: %s", path)
        return {"camera_name": block_id, "frame_width": 0, "frame_height": 0,
                "road_roi": [], "low_point_roi": [], "lane_threshold_line": []}


def _clean_polygons(value, *, w: int, h: int, min_points: int) -> list:
    """좌표를 정수로 맞추고 프레임 밖 값을 잘라낸다.

    프레임을 벗어난 좌표가 저장되면 마스크 연산에서 조용히 잘려, 화면에 보이는
    영역과 실제 판정 영역이 달라진다. 저장 시점에 맞춰 둔다.
    """
    out = []
    for poly in value or []:
        pts = []
        for p in poly or []:
            try:
                x, y = int(round(float(p[0]))), int(round(float(p[1])))
            except (TypeError, ValueError, IndexError):
                continue
            if w > 0:
                x = max(0, min(w - 1, x))
            if h > 0:
                y = max(0, min(h - 1, y))
            pts.append([x, y])
        if len(pts) >= min_points:
            out.append(pts)
    return out


def validate(payload: dict) -> list[str]:
    errs: list[str] = []
    try:
        w = int(payload.get("frame_width") or 0)
        h = int(payload.get("frame_height") or 0)
    except (TypeError, ValueError):
        return ["프레임 크기가 올바르지 않습니다."]
    if w <= 0 or h <= 0:
        errs.append("정지영상 크기를 확인할 수 없습니다. 영상을 다시 불러오세요.")

    road = _clean_polygons(payload.get("road_roi"), w=w, h=h, min_points=3)
    if not road:
        errs.append("도로 ROI는 꼭짓점 3개 이상인 다각형이 최소 하나 필요합니다.")

    line = payload.get("lane_threshold_line") or []
    if line and len(line) != 2:
        errs.append("침수 경계선은 점 2개로 이뤄져야 합니다. 지우고 다시 그리세요.")
    return errs


def save(block_id: str, payload: dict) -> tuple[dict | None, list[str], Path | None]:
    errs = validate(payload)
    if errs:
        return None, errs, None

    w = int(payload["frame_width"])
    h = int(payload["frame_height"])
    data = {
        "camera_name": block_id,
        "frame_width": w,
        "frame_height": h,
        "road_roi": _clean_polygons(payload.get("road_roi"), w=w, h=h, min_points=3),
        "low_point_roi": _clean_polygons(payload.get("low_point_roi"),
                                         w=w, h=h, min_points=3),
        "lane_threshold_line": _clean_polygons([payload.get("lane_threshold_line")],
                                               w=w, h=h, min_points=2),
    }
    # 침수 경계선은 단일 점열이라 한 겹 벗긴다.
    data["lane_threshold_line"] = (data["lane_threshold_line"][0]
                                   if data["lane_threshold_line"] else [])

    path = roi_path(block_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = backup(path) if path.exists() else None
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    log.info("ROI 저장 block=%s road=%d low=%d line=%d", block_id,
             len(data["road_roi"]), len(data["low_point_roi"]),
             len(data["lane_threshold_line"]))
    return data, [], bak


def status(block_ids: list[str]) -> dict[str, dict]:
    """지점별 ROI 설정 현황."""
    out = {}
    for bid in block_ids:
        d = load(bid)
        out[bid] = {
            "exists": roi_path(bid).exists(),
            "road": len(d.get("road_roi") or []),
            "low": len(d.get("low_point_roi") or []),
            "line": bool(d.get("lane_threshold_line")),
        }
    return out
