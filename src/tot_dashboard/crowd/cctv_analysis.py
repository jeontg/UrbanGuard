"""인파 — 선택한 CCTV를 일정 시간 관측해 분석한다 (S-30 선택 탐지).

전 지점 상시 분석은 CPU가 감당하지 못한다. 인파 검출은 타일 분할 추론이라
카메라 한 대에도 부하가 크다. 그래서 **관제요원이 카메라를 고를 때만** 돌린다.

관측 시간을 30초로 둔 이유 — 배회 판정은 체류 시간(기본 60초)을 보므로 짧으면
의미가 없고, 60초는 관제요원이 화면 앞에서 기다리기에 길다. 30초면 밀집도·
이동 패턴은 충분히 잡히고, 배회는 「징후」 수준으로 확인된다.

⚠️ 벽시계 시간 기준으로 창을 관리한다 — HLS는 버퍼 구간을 실시간보다 빠르게
디코딩할 수 있어, 프레임 수만 세면 실제 경과 시간과 어긋난다(도로 도메인에서
같은 문제를 겪었다).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("urbanguard.crowd.cctv")

DURATION_SEC = 30.0
SAMPLE_INTERVAL_SEC = 1.0


@dataclass
class CrowdCctvResult:
    camera_id: str
    camera_name: str
    frames_analyzed: int = 0
    duration_sec: float = 0.0
    people_max: int = 0
    people_avg: float = 0.0
    density_max: float = 0.0
    events: list[dict] = field(default_factory=list)
    risk_level: str = ""
    risk_score: float = 0.0
    severity: int = 0
    snapshot: str = ""
    mask_status: str = ""
    # 사람 검출 소스. mock 이면 **카메라 영상이 아니라 모의 데이터**로 나온
    # 결과다. 화면에서 반드시 구분해 보여 줘야 한다.
    source: str = ""
    note: str = ""
    # 지면 평면을 보정한 지점에서만 값이 있다. 국내외 기준(3·4·5명/㎡)과
    # 연결되는 유일한 단위다. 보정 전에는 None / "미보정".
    per_m2: float | None = None
    crowd_level: str = "미보정"

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id, "camera_name": self.camera_name,
            "frames_analyzed": self.frames_analyzed,
            "duration_sec": round(self.duration_sec, 1),
            "people_max": self.people_max, "people_avg": round(self.people_avg, 1),
            "density_max": round(self.density_max, 3),
            "events": self.events, "risk_level": self.risk_level,
            "risk_score": round(self.risk_score, 3), "severity": self.severity,
            "snapshot": self.snapshot, "mask_status": self.mask_status,
            "source": self.source, "note": self.note,
            "per_m2": self.per_m2, "crowd_level": self.crowd_level,
        }


def _stream_url(cam) -> str:
    if getattr(cam, "source_type", "") == "hls":
        return getattr(cam, "source_url", "") or ""
    if getattr(cam, "source_type", "") == "video":
        return getattr(cam, "source_path", "") or ""
    return ""


def _density_per_m2(cam, people: int) -> tuple[float | None, str]:
    """(명/㎡, 단계). 지면 평면을 보정하지 않았으면 (None, "미보정").

    분석 영역(ROI)이 지정돼 있으면 그 영역의 실제 바닥 면적을 쓰고, 없으면
    보정에 쓴 네 점이 이루는 영역을 쓴다. **화면 전체를 면적으로 삼지
    않는다** — 하늘·건물이 포함되면 밀도가 실제보다 훨씬 낮게 나온다.
    """
    try:
        from ..core import calibration
        from ..core import cameras as _cams
        from ..core.db import get_session

        cal = calibration.of(cam, "crowd")
        if cal.ground is None:
            return None, "미보정"

        polygon = None
        roi = cam.roi_row("crowd") if hasattr(cam, "roi_row") else None
        shapes = (roi.shapes if roi else None) or {}
        for key in ("analysis_roi", "loiter_roi", "intrusion_roi"):
            shape = shapes.get(key)
            if shape:
                # ROI 는 [[polygon], ...] 형태로 저장된다.
                polygon = shape[0] if isinstance(shape[0][0], (list, tuple)) else shape
                break
        if not polygon:
            polygon = cal.ground.image_points

        per = calibration.crowd_per_m2(cal, people, polygon)
        # 등급 구간 표(S-95)를 보려면 세션이 필요하다. 실패하면 코드 기본값으로
        # 되돌아가므로 여기서 막히지 않는다.
        _db = None
        try:
            _db = get_session()
            level = calibration.crowd_level(per, db=_db)
        finally:
            if _db is not None:
                _db.close()
        return (round(per, 2) if per is not None else None, level)
    except Exception:  # noqa: BLE001
        log.debug("인파 밀도 보정 실패", exc_info=True)
        return None, "미보정"


def analyze(cam, analyzer, *, duration_sec: float = DURATION_SEC,
            sample_interval_sec: float = SAMPLE_INTERVAL_SEC) -> CrowdCctvResult:
    """카메라를 ``duration_sec`` 동안 관측하며 분석기에 프레임을 넣는다.

    ``analyzer`` 는 :class:`~..crowd.live_analyzer.CrowdLiveAnalyzer` 로,
    ``step(t_sec, frame_bgr)`` 에 프레임을 주입받는 구조라 그대로 재사용한다.
    """
    res = CrowdCctvResult(camera_id=getattr(cam, "id", "?"),
                          camera_name=getattr(cam, "name", "") or "")
    url = _stream_url(cam)
    if not url:
        res.note = "이 카메라는 영상 소스가 없습니다."
        return res

    try:
        import cv2
    except Exception:  # noqa: BLE001
        res.note = "영상 라이브러리를 쓸 수 없습니다."
        return res

    t0 = time.time()
    # 스트림은 매번 새로 연다 — 캐시된 오래된 프레임을 피한다.
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        res.note = "스트림을 열지 못했습니다. 주소와 네트워크를 확인하세요."
        return res

    # ⚠️ 분석기는 상시 루프와 **같은 객체**를 공유한다. 시작 시점에 이미 쌓여
    # 있던 이벤트를 이번 관측 결과로 내보내면, 30초 관측에 「체류 190초」 같은
    # 앞뒤가 맞지 않는 값이 섞인다. 미리 서명을 떠서 걸러 낸다.
    def _sig(e: dict) -> tuple:
        return (e.get("eventType"), e.get("trackId") or e.get("track_id"))

    seen: set[tuple] = set()
    for e0 in (getattr(analyzer, "_recent_events", None) or []):
        seen.add(_sig(e0.to_dict() if hasattr(e0, "to_dict") else e0))

    counts: list[int] = []
    densities: list[float] = []
    events: list[dict] = []
    last_frame = None
    deadline = t0 + max(duration_sec, 1.0)
    next_sample = 0.0
    try:
        while time.time() < deadline:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            elapsed = time.time() - t0
            if elapsed < next_sample:
                continue
            next_sample = elapsed + sample_interval_sec
            last_frame = frame
            try:
                snap = analyzer.step(elapsed, frame)
            except Exception:  # noqa: BLE001
                log.exception("인파 분석 중 오류 camera=%s", res.camera_id)
                continue
            res.frames_analyzed += 1
            d = snap.to_dict() if hasattr(snap, "to_dict") else {}
            if d.get("person_count") is not None:
                counts.append(int(d["person_count"]))
            if d.get("density_index") is not None:
                densities.append(float(d["density_index"]))
            # 같은 이벤트가 매 프레임 반복되므로 중복을 걷어낸다.
            for e in (d.get("events") or []):
                sig = _sig(e)
                if sig not in seen:
                    seen.add(sig)
                    events.append(e)
            res.source = d.get("source") or res.source
            # 30초 관측의 대표값은 **가장 위험했던 순간**이다. 마지막 값을 쓰면
            # 위험이 지나간 뒤 「정상」으로 덮여 관제요원이 놓친다.
            sev = int(d.get("severity") or 0)
            if d.get("risk_name") and (sev > res.severity or not res.risk_level):
                res.severity = sev
                res.risk_level = d["risk_name"]
            res.risk_score = max(res.risk_score, float(d.get("risk_score") or 0))
    finally:
        cap.release()

    res.duration_sec = time.time() - t0
    if not res.frames_analyzed:
        res.note = "스트림에서 프레임을 받지 못했습니다."
        return res

    res.people_max = max(counts) if counts else 0
    res.people_avg = (sum(counts) / len(counts)) if counts else 0.0
    res.density_max = max(densities) if densities else 0.0
    res.events = events

    # 지면 평면을 보정한 지점이면 **명/㎡** 를 함께 낸다.
    # 국내외 기준(3·4·5명/㎡)은 전부 이 단위이며, 화면 격자 점유율(%)로는
    # 그 기준과 연결할 수 없다(docs/202608151713/threshold_rationale.md).
    # 보정 전에는 None 이고 단계는 「미보정」 — 보정한 척하지 않는다.
    res.per_m2, res.crowd_level = _density_per_m2(cam, res.people_max)

    if last_frame is not None:
        # 스냅샷에는 사람이 그대로 찍힌다. 관제 화면에 원본을 띄우면 개인 식별이
        # 가능하므로, 제보 사진과 **같은 기준으로 가려서** 내보낸다(설계서 9-5절).
        # 가리지 못했으면 화면에 띄우지 않는다 — 상태만 알려 판단을 맡긴다.
        try:
            import base64

            from ..core import image_mask
            status, _ = image_mask.mask_array(last_frame)
            res.mask_status = status
            if status in image_mask.NEEDS_REVIEW:
                log.warning("스냅샷 마스킹 실패(%s) — 화면에 내보내지 않는다 camera=%s",
                            status, res.camera_id)
            else:
                ok, buf = cv2.imencode(".jpg", last_frame)
                if ok:
                    res.snapshot = base64.b64encode(buf.tobytes()).decode("ascii")
        except Exception:  # noqa: BLE001
            log.exception("스냅샷 처리 중 오류 camera=%s", res.camera_id)
            res.mask_status = "failed"

    log.info("인파 선택 분석 camera=%s frames=%d people_max=%d events=%d",
             res.camera_id, res.frames_analyzed, res.people_max, len(events))
    return res
