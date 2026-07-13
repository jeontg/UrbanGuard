"""PreemptiveAction — builds the alert payload (hotspot, recommendation,
alert-log). Pure logic, no GPU dependency.

Ported from SAM's ``SAM3_install.py`` (Stage 5).
"""
from __future__ import annotations

import json

import numpy as np

from .config import CCTV_ID, LOCATION_NAME

RECOMMEND = {
    "NORMAL": "정상 모니터링 유지",
    "CROWD_DENSITY_HIGH": "밀집 구역 진입 통제 검토 / 안내방송 준비",
    "FLOW_CHAOS": "현장 CCTV 정밀 관찰 / 유도요원 대기 (역류·병목 주시)",
    "CROWD_SURGE_RISK": "관할 기관 순찰 강화 권고 / 핫스팟 공유",
    "PANIC_DISPERSION": "[긴급] 즉시 순찰 출동 + 대피동선 확보 + 통제센터 보고",
}


class PreemptiveAction:
    def __init__(self, location: str = LOCATION_NAME, alert_min_sev: int = 3):
        self.location = location
        self.alert_min_sev = alert_min_sev
        self.alert_log: list[dict] = []

    @staticmethod
    def hotspot(density_map: np.ndarray) -> dict | None:
        if density_map.max() <= 0:
            return None
        y, x = np.unravel_index(np.argmax(density_map), density_map.shape)
        return {"x": int(x), "y": int(y), "intensity": float(density_map.max())}

    def act(self, t_sec: float, frame_idx: int, risk: dict, density_map: np.ndarray,
            vlm_full: dict | None = None) -> dict:
        payload = {
            "timestamp_sec": round(t_sec, 2), "frame": frame_idx,
            "location": self.location, "cctv_id": CCTV_ID,
            "risk_code": risk["risk_code"], "risk_name": risk["risk_name_kr"],
            "severity": risk["severity"], "risk_score": risk["score"],
            "warning": (vlm_full or {}).get("warning_ko", risk["context"]),
            "warning_en": (vlm_full or {}).get("warning_en", ""),
            "density_grade": (vlm_full or {}).get("density_grade", ""),
            "movement": (vlm_full or {}).get("movement", ""),
            "confidence": (vlm_full or {}).get("confidence", ""),
            "escalation": (vlm_full or {}).get("escalation", ""),
            "drivers": risk["drivers"],
            "hotspot": self.hotspot(density_map),
            "recommendation": (vlm_full or {}).get("recommended_action", "")
                              or RECOMMEND[risk["risk_code"]],
            "alert": risk["severity"] >= self.alert_min_sev,
        }
        if payload["alert"]:
            self.alert_log.append(payload)
        return payload

    def save(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.alert_log, f, ensure_ascii=False, indent=2)
        return path
