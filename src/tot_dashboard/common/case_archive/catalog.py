"""Static case-manifest catalog (SAM's dashboard archive style).

Ported from SAM's ``app/catalog.py``. Reads pre-built ``data/cases/<id>/
manifest.json`` bundles (originally: SAM3 results computed in Google Colab and
copied in via ``scripts/build_case.py``) and safely serves the referenced
media files by id.

★ Kept as a SEPARATE archival mechanism from ``run_writer.py``, not merged
into it — see that module's docstring and docs/integration_plan.md section
3-5. ``_build_sms_messages``'s wording was genericized (dropped the original
"군중" / crowd-specific phrasing) so any case with an ``alerts.json`` in the
same shape can use it, but its ``severity_labels`` dict still assumes a 0-4
integer scale matching SAM3's ``RISK_TAXONOMY`` severity — flood's alert_level
(1-5) / risk_grade (1-5) do NOT line up with these keys numerically. A domain
whose severity scale differs should build its own alerts.json with a 0-4
``severity`` field, or write its own message builder, rather than assuming
this one adapts automatically.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class CatalogError(RuntimeError):
    """Raised when case metadata cannot be loaded safely."""


class CaseCatalog:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()

    def list_cases(self) -> list[dict[str, Any]]:
        if not self.data_root.exists():
            return []

        cases: list[dict[str, Any]] = []
        for manifest_path in sorted(self.data_root.glob("*/manifest.json")):
            try:
                manifest = self._load_manifest(manifest_path)
            except (CatalogError, OSError, json.JSONDecodeError):
                continue

            summary = manifest.get("summary", {})
            cases.append(
                {
                    "id": manifest["id"],
                    "title": manifest.get("title", manifest["id"]),
                    "location": manifest.get("location", "Unknown location"),
                    "status": manifest.get("status", "ready"),
                    "captured_at": manifest.get("captured_at"),
                    "highest_severity": summary.get("highest_severity", 0),
                    "alert_count": summary.get("alert_count", 0),
                    "thumbnail_url": self._thumbnail_url(manifest),
                }
            )
        return cases

    def get_case(self, case_id: str) -> dict[str, Any]:
        case_root = self.case_root(case_id)
        manifest_path = case_root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(case_id)

        manifest = self._load_manifest(manifest_path)
        payload = deepcopy(manifest)
        assets = payload.get("assets", {})

        for asset in assets.values():
            relative_path = asset.get("path")
            if relative_path:
                media_path = self.resolve_media(case_id, relative_path)
                asset["available"] = media_path.is_file()
                asset["url"] = self.media_url(case_id, relative_path)
                if asset["available"]:
                    asset["url"] += f"?v={media_path.stat().st_mtime_ns}"

        alerts = self._load_alerts(case_id, assets)
        payload["alerts"] = alerts
        payload["sms_messages"] = self._build_sms_messages(alerts)
        payload["available_asset_count"] = sum(
            1 for asset in assets.values() if asset.get("available")
        )
        return payload

    def case_root(self, case_id: str) -> Path:
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise FileNotFoundError(case_id)
        root = (self.data_root / case_id).resolve()
        if root.parent != self.data_root:
            raise FileNotFoundError(case_id)
        return root

    def resolve_media(self, case_id: str, relative_path: str) -> Path:
        root = self.case_root(case_id)
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise FileNotFoundError(relative_path)
        return candidate

    @staticmethod
    def media_url(case_id: str, relative_path: str) -> str:
        safe_path = "/".join(quote(part) for part in Path(relative_path).parts)
        return f"/media/{quote(case_id)}/{safe_path}"

    def _load_manifest(self, manifest_path: Path) -> dict[str, Any]:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        case_id = manifest.get("id")
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            raise CatalogError(f"Invalid case id in {manifest_path}")
        if manifest_path.parent.name != case_id:
            raise CatalogError(
                f"Manifest id '{case_id}' does not match folder '{manifest_path.parent.name}'"
            )
        if not isinstance(manifest.get("assets", {}), dict):
            raise CatalogError(f"Invalid assets object in {manifest_path}")
        return manifest

    def _load_alerts(
        self, case_id: str, assets: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        alert_asset = assets.get("alerts")
        if not alert_asset or not alert_asset.get("path"):
            return []

        alert_path = self.resolve_media(case_id, alert_asset["path"])
        if not alert_path.is_file():
            return []

        try:
            alerts = json.loads(alert_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return alerts if isinstance(alerts, list) else []

    @staticmethod
    def _build_sms_messages(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        severity_labels = {
            0: "관심",
            1: "주의",
            2: "주의",
            3: "경계",
            4: "심각",
        }
        messages: list[dict[str, Any]] = []

        for index, alert in enumerate(alerts, start=1):
            if not alert.get("alert", True):
                continue

            severity = int(alert.get("severity", 0) or 0)
            level = severity_labels.get(severity, "경보")
            location = str(alert.get("location") or "해당 구역")
            context = str(alert.get("context") or alert.get("risk_name") or "위험이 감지되었습니다.")
            recommendation = str(
                alert.get("recommendation") or "현장 상황을 확인해 주십시오."
            )
            timestamp = float(alert.get("timestamp_sec", 0) or 0)
            score = float(alert.get("risk_score", 0) or 0)
            minutes = int(timestamp // 60)
            seconds = timestamp - minutes * 60
            video_time = f"{minutes:02d}:{seconds:05.2f}"

            body = (
                f"[{level}] {location} 위험\n"
                f"{context}\n"
                f"조치: {recommendation}\n"
                f"영상 {video_time} · 위험점수 {score:.3f}"
            )
            messages.append(
                {
                    "id": f"sms-{index:03d}",
                    "alert_index": index - 1,
                    "title": f"{level} · {alert.get('risk_name') or '위험'}",
                    "sender": "통합관제센터",
                    "recipient": "현장 대응 담당자",
                    "severity": severity,
                    "severity_label": level,
                    "location": location,
                    "context": context,
                    "recommendation": recommendation,
                    "timestamp_sec": round(timestamp, 2),
                    "video_time": video_time,
                    "frame": alert.get("frame"),
                    "risk_score": round(score, 3),
                    "body": body,
                    "character_count": len(body),
                }
            )
        return messages

    def _thumbnail_url(self, manifest: dict[str, Any]) -> str | None:
        assets = manifest.get("assets", {})
        for key in ("input_image", "image_heatmap", "image_segmentation", "timeline"):
            asset = assets.get(key)
            if asset and asset.get("path"):
                return self.media_url(manifest["id"], asset["path"])
        return None
