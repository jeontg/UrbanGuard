"""인파 위험행동 실시간 분석 — 추적 → 배회/침입 + 군중지표 → 위험도.

## 위치

기존 crowd 도메인은 **미리 계산된 케이스를 재생하는 뷰어**였다
(`data/cases/*/manifest.json`의 ``"local_mode": "분석 산출물 조회 전용"``).
이 모듈은 그와 별개로, 대시보드에서 **실시간 분석 결과를 보여주기 위한** 경로다.

## 이중 모드 (사용자 요청)

현장 장비·GPU가 없는 상태에서 기능을 개발해야 하므로,
``field_sensors.py``와 동일하게 **소스를 설정으로 고르는** 구조로 만들었다.

  - ``mock``     : 합성 궤적으로 동작 (검출 모델 없이 지금 시연 가능)
  - ``detector`` : 실제 사람 검출기 연동 (torchvision BSD, CPU 가능)

상위 로직(배회·침입 판정, 위험도 산정)은 어느 쪽인지 모른다. 검출기를 붙이는
시점에 설정만 바꾸면 된다.

⚠️ ``detector`` 모드는 **torchvision 사전학습 가중치 다운로드(최초 1회)** 가
필요하고, 아직 실제 검출 정확도가 검증되지 않았다. 기본값은 ``mock``이다.

## 산출물

사양 ②의 출력 예시(``dwell_time``/``trajectory_variance``/``density_index``/
``action_label``)에 대응하는 값을 낸다.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..common.roi import point_in_polygons, scale_polygons
from .behavior_events import BehaviorEvent, BehaviorEventDetector
from .field_sensors import DepthState, EnvironmentState, build_depth, build_environment
from .heatmap import grid_density


# ─────────────────────────────────────────────────────────────
# 소스: 사람 박스를 어디서 얻을 것인가
# ─────────────────────────────────────────────────────────────
# mock 장면 배치(비율). 통제구역은 통행로와 **겹치지 않게** 배치했다 —
# 겹치면 지나가는 통행자가 전부 침입으로 잡혀 시연이 무의미해진다(실측 확인:
# 통행로와 겹쳤을 때 60초에 침입 23건 발생).
MOCK_WALK_BAND = (0.55, 0.67)      # 통행자 y 범위(화면 높이 비율)
MOCK_RESTRICTED_ROI = [[[0.62, 0.80], [0.95, 0.80], [0.95, 0.99], [0.62, 0.99]]]
MOCK_LOITER_POS = (0.22, 0.75)     # 배회자 위치


def mock_restricted_roi(width: int = 1280, height: int = 720) -> list:
    """mock 장면에 맞는 통제구역 ROI(픽셀 좌표). 통행로와 겹치지 않는다."""
    return [[[int(x * width), int(y * height)] for x, y in poly]
            for poly in MOCK_RESTRICTED_ROI]


class MockCrowdSource:
    """합성 궤적 생성 — 검출 모델 없이 기능 시연·검증용.

    실제 관제에서 관심 있는 4가지 상황을 **의도적으로** 재현한다:
      1. **배회자** — 한 자리에 계속 머무는 1명 (배회로 잡혀야 함)
      2. **통행자** — 화면을 가로지르는 다수 (배회로 잡히면 **안 됨**)
      3. **침입자** — 주기적으로 통제구역에 들어갔다 나오는 1명
      4. **군중 급증** — 인원이 늘었다 줄어듦

    ⚠️ 합성 데이터이므로 **검출 정확도 평가에는 쓸 수 없다.** 상위 로직(배회·
    침입 판정, 위험도 산정)이 올바로 동작하는지 확인하는 용도다.
    """

    def __init__(self, width: int = 1280, height: int = 720,
                 base_people: int = 8, surge_people: int = 26,
                 surge_period_sec: float = 60.0,
                 intruder_period_sec: float = 40.0, seed: int = 0):
        self.w = width
        self.h = height
        self.base = base_people
        self.surge = surge_people
        self.period = surge_period_sec
        self.intruder_period = intruder_period_sec
        self._rng = np.random.default_rng(seed)

    def boxes_at(self, t_sec: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(boxes[N,4], scores[N], ids[N]) 반환."""
        frac = 0.5 * (1 - math.cos(2 * math.pi * t_sec / max(self.period, 1)))
        n_walk = int(self.base + (self.surge - self.base) * frac)

        boxes, ids = [], []

        # 1) 배회자 — 거의 고정(작은 흔들림만)
        jitter = self._rng.normal(0, 3, 2)
        cx = self.w * MOCK_LOITER_POS[0] + jitter[0]
        cy = self.h * MOCK_LOITER_POS[1] + jitter[1]
        boxes.append([cx - 22, cy - 130, cx + 22, cy])
        ids.append(1)

        # 2) 통행자 — 좌->우 이동. 통제구역 위쪽 띠로만 다녀 침입에 걸리지 않는다
        lo, hi = MOCK_WALK_BAND
        for k in range(n_walk):
            speed = 40 + (k % 5) * 18
            x = (t_sec * speed + k * 137) % (self.w + 200) - 100
            y = self.h * (lo + (hi - lo) * ((k * 7) % 5) / 5.0)
            hgt = 120 * (0.8 + 0.4 * ((k * 3) % 4) / 4.0)
            boxes.append([x - hgt * 0.17, y - hgt, x + hgt * 0.17, y])
            ids.append(100 + k)

        # 3) 침입자 — 통제구역을 **연속적으로** 드나든다.
        #    순간이동시키면 ByteTrack이 다른 사람으로 인식해 새 ID를 부여하고,
        #    새 ID는 "최초 관측 시 이미 안에 있음"이 되어 진입으로 잡히지 않는다
        #    (실측으로 확인: 순간이동 방식일 때 침입 탐지 0건).
        #    사인파로 위<->아래를 왕복시켜 밖->안 전환이 실제로 관측되게 한다.
        ph = 2 * math.pi * t_sec / max(self.intruder_period, 1)
        iy = self.h * (0.81 + 0.10 * math.sin(ph))   # 0.71 ~ 0.91 왕복
        ix = self.w * 0.78
        boxes.append([ix - 22, iy - 130, ix + 22, iy])
        ids.append(2)

        arr = np.asarray(boxes, np.float32)
        scores = np.full(len(arr), 0.85, np.float32)
        return arr, scores, np.asarray(ids, np.int32)


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_th: float) -> tuple[np.ndarray, np.ndarray]:
    """타일 경계에서 생기는 중복 박스 제거."""
    if len(boxes) == 0:
        return boxes, scores
    idx = scores.argsort()[::-1]
    keep = []
    while len(idx):
        i = idx[0]
        keep.append(i)
        if len(idx) == 1:
            break
        b, r = boxes[i], boxes[idx[1:]]
        xx1 = np.maximum(b[0], r[:, 0]); yy1 = np.maximum(b[1], r[:, 1])
        xx2 = np.minimum(b[2], r[:, 2]); yy2 = np.minimum(b[3], r[:, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        ab = (b[2] - b[0]) * (b[3] - b[1])
        ar = (r[:, 2] - r[:, 0]) * (r[:, 3] - r[:, 1])
        iou = inter / (ab + ar - inter + 1e-6)
        idx = idx[1:][iou < iou_th]
    k = np.array(keep)
    return boxes[k], scores[k]


class DetectorCrowdSource:
    """실제 사람 검출기(torchvision, BSD) 기반 소스.

    ## 타일 분할 추론 (기본 활성)

    인파 장면은 원경 인물이 작아 전체 이미지 1회 추론으로는 크게 놓친다.
    **데모 케이스(정답 39명)로 실측한 결과**:

    | 방식 | 검출 | 정답대비 | 소요 |
    |---|---|---|---|
    | SSDLite 전체 1회 | 8명 | 21% | 0.2s |
    | FasterRCNN 전체 1회 | 16명 | 41% | 0.4s |
    | **FasterRCNN 2x2 타일 (conf 0.4 / NMS 0.4)** | **39명** | **100%** | **1.6s** |

    → 화면을 격자로 나눠 각각 추론한 뒤 좌표를 복원하고 NMS로 병합한다.
    연산은 늘지만 관제는 실시간 30fps가 불필요하므로(프레임 샘플링) 감당 가능하다.

    ⚠️ **정답 39명은 SAM3 결과이지 사람이 센 수가 아니다.** "정확도 100%"가
    아니라 "SAM3와 동일한 인원수"라는 뜻이며, 개별 박스가 같은 사람을 가리키는지는
    검증하지 않았다. 또한 **이 한 장면에 맞춘 값**이므로 다른 장면·야간·저해상도
    CCTV에서는 재조정이 필요하다.

    ⚠️ 최초 사용 시 사전학습 가중치를 내려받는다(FasterRCNN 약 74MB).

    추적 ID는 여기서 만들지 않는다(검출 전담). ID 부여는 상위
    ``CrowdBehaviorTracker``(ByteTrack)가 한다.
    """

    COCO_PERSON = 1

    def __init__(self, arch: str = "fasterrcnn", conf: float = 0.4,
                 threads: int | None = None,
                 tile_grid: tuple[int, int] | None = (2, 2),
                 tile_overlap: float = 0.2, nms_iou: float = 0.4):
        # ⚠️ 2026-09-02 — 예전엔 여기 하드코딩된 8이었다. `scripts/serve.py`가
        # 속도 개선 1단계로 서비스당 OMP_NUM_THREADS를 코어 수에 맞춰
        # 나눠 주기 시작했는데, 이 클래스가 그와 무관하게 "나 혼자 8개
        # 쓰겠다"고 하면 그 조정이 무의미해진다 — 같은 환경변수를
        # 따라가게 해 항상 정합을 유지한다(호출부 어디도 `threads=`를
        # 명시적으로 넘기지 않으므로 이 기본값 변경만으로 충분하다).
        if threads is None:
            threads = int(os.environ.get("OMP_NUM_THREADS", "4"))
        import torch
        from torchvision.models.detection import (fasterrcnn_mobilenet_v3_large_fpn,
                                                  ssdlite320_mobilenet_v3_large)
        torch.set_num_threads(threads)
        builder = (ssdlite320_mobilenet_v3_large if arch == "ssdlite"
                   else fasterrcnn_mobilenet_v3_large_fpn)
        self._torch = torch
        self.model = builder(weights="DEFAULT")
        self.model.eval()
        self.conf = conf
        self.tile_grid = tile_grid          # None이면 전체 1회 추론
        self.tile_overlap = tile_overlap
        self.nms_iou = nms_iou

    def _infer(self, bgr) -> tuple[np.ndarray, np.ndarray]:
        """단일 이미지(또는 타일) 추론 -> 사람 박스만."""
        rgb = bgr[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        x = [self._torch.from_numpy(np.ascontiguousarray(rgb))]
        with self._torch.no_grad():
            out = self.model(x)[0]
        keep = (out["labels"] == self.COCO_PERSON) & (out["scores"] >= self.conf)
        return (out["boxes"][keep].cpu().numpy(), out["scores"][keep].cpu().numpy())

    def boxes_from_frame(self, frame_bgr) -> tuple[np.ndarray, np.ndarray]:
        """BGR 프레임 -> (boxes[N,4], scores[N]). 사람 클래스만."""
        if not self.tile_grid:
            return self._infer(frame_bgr)

        H, W = frame_bgr.shape[:2]
        rows, cols = self.tile_grid
        th, tw = H // rows, W // cols
        oy, ox = int(th * self.tile_overlap), int(tw * self.tile_overlap)
        bs, ss = [], []
        for r in range(rows):
            for c in range(cols):
                y0, y1 = max(0, r * th - oy), min(H, (r + 1) * th + oy)
                x0, x1 = max(0, c * tw - ox), min(W, (c + 1) * tw + ox)
                b, s = self._infer(frame_bgr[y0:y1, x0:x1])
                if len(b):
                    b = b.copy()
                    b[:, [0, 2]] += x0        # 타일 좌표 -> 원본 좌표 복원
                    b[:, [1, 3]] += y0
                    bs.append(b); ss.append(s)
        if not bs:
            return np.zeros((0, 4), np.float32), np.zeros(0, np.float32)
        return _nms(np.vstack(bs), np.concatenate(ss), self.nms_iou)


# ─────────────────────────────────────────────────────────────
# 분석 결과
# ─────────────────────────────────────────────────────────────
@dataclass
class CrowdSnapshot:
    """대시보드 1회 갱신에 필요한 전체 상태."""
    t_sec: float
    person_count: int
    density_index: float                      # 사양 ②의 density_index
    mean_speed: float
    trajectory_variance: float                # 사양 ②의 trajectory_variance
    action_label: str                         # 사양 ②의 action_label
    risk_code: str
    risk_name: str
    risk_score: float
    severity: int
    # --- 흐름 지표와 판정 근거 -------------------------------------------
    #
    # 넷 다 **이미 위험도 산출에 쓰이고 있었다**(밀집 35% + 급증 25% +
    # 분산 20% + 발산 20%). 그런데 스냅샷이 담지 않아 관제요원은 「군중급증
    # 0.72」라는 결과만 보고 **왜 그런지는 볼 수 없었다.**
    #
    # 근거를 못 보면 판단을 검증할 수 없고, 오탐인지 실제인지 가릴 수도 없다.
    surge: float = 1.0          # 평소 대비 속도 배수. 1.0이면 평상시
    divergence: float = 0.0     # 양수면 중심에서 퍼짐, 음수면 모임
    drivers: list[str] = field(default_factory=list)   # 「발산도UP」 같은 근거
    events: list[BehaviorEvent] = field(default_factory=list)
    env: EnvironmentState | None = None
    depth: DepthState | None = None
    source: str = "mock"

    def to_dict(self) -> dict[str, Any]:
        return {
            "t_sec": round(self.t_sec, 1),
            "person_count": self.person_count,
            "density_index": round(self.density_index, 2),
            "mean_speed": round(self.mean_speed, 1),
            "trajectory_variance": round(self.trajectory_variance, 3),
            "surge": round(self.surge, 2),
            "divergence": round(self.divergence, 2),
            "drivers": list(self.drivers),
            "action_label": self.action_label,
            "risk_code": self.risk_code, "risk_name": self.risk_name,
            "risk_score": round(self.risk_score, 3), "severity": self.severity,
            "events": [e.to_dict() for e in self.events],
            "environment": self.env.to_dict() if self.env else None,
            "depth": self.depth.to_dict() if self.depth else None,
            "source": self.source,
        }


# 사양의 탐지 대상 중 이 모듈이 판정하는 범위
_ACTION_BY_RISK = {
    "NORMAL": "정상",
    "CROWD_DENSITY_HIGH": "군중밀집",
    "FLOW_CHAOS": "이동흐름혼란",
    "CROWD_SURGE_RISK": "군중급증(CrowdSurge)",
    "PANIC_DISPERSION": "패닉움직임(Panic-like)",
}


class CrowdLiveAnalyzer:
    """소스 → 추적 → 배회/침입 + 군중지표 → 위험도.

    ``blocks.json``의 ``crowd`` 설정을 받는다::

        "crowd": {
          "source": {"type": "mock"},              // 또는 {"type":"detector","arch":"ssdlite"}
          "environment": {"type": "mock"},          // 또는 {"type":"device","url":...}
          "stereo": {"type": "mock"},
          "loiter_sec": 120, "commercial_zone": true,
          "intrusion_roi": [[[x,y],...]]
        }
    """

    def __init__(self, cfg: dict | None = None, block_id: str | None = None,
                 node_id: str | None = None, width: int = 1280, height: int = 720,
                 fps: float = 5.0):
        cfg = cfg or {}
        self.cfg = cfg
        self.w, self.h = width, height
        self.block_id = block_id
        self.node_id = node_id

        src_cfg = cfg.get("source") or {"type": "mock"}
        self.source_type = src_cfg.get("type", "mock")
        self.source: Any = None
        if self.source_type == "detector":
            try:
                # 기본값은 데모 케이스로 실측 튜닝한 조합(2x2 타일 / conf 0.4 / NMS 0.4)
                grid = src_cfg.get("tile_grid", [2, 2])
                self.source = DetectorCrowdSource(
                    arch=src_cfg.get("arch", "fasterrcnn"),
                    conf=src_cfg.get("conf", 0.4),
                    tile_grid=tuple(grid) if grid else None,
                    nms_iou=src_cfg.get("nms_iou", 0.4))
            except Exception as e:  # noqa: BLE001
                print(f"[crowd] 검출기 로드 실패 -> mock 사용: {str(e)[:100]}")
                self.source_type = "mock"
        if self.source is None:
            self.source = MockCrowdSource(width=width, height=height,
                                          base_people=src_cfg.get("base_people", 8),
                                          surge_people=src_cfg.get("surge_people", 26),
                                          surge_period_sec=src_cfg.get("surge_period_sec", 60.0))

        self.env_provider = build_environment(cfg.get("environment"))
        self.depth_provider = build_depth(cfg.get("stereo"))

        from .behavior_tracker import CrowdBehaviorTracker
        self.tracker = CrowdBehaviorTracker(fps=fps)

        # ⚠️ 2026-08-23 실사용 중 발견(SEOUL-1042 ROI 미표시 건 점검 중
        # 다른 카메라에서도 확인) — ROI 는 **그린 정지영상 해상도** 기준
        # 좌표인데, 이 분석기가 실제 판정에 쓰는 (width, height)가 그것과
        # 다르면(스트림 화질 변경 등) 좌표를 그대로 쓰면 화면 밖 엉뚱한
        # 자리를 침입/배회 구역으로 착각한다 — traffic_tracker.py/
        # flood/metrics_core.py 와 같은 이유로 같은 scale_polygons() 를 쓴다.
        _roi_from_wh = (cfg.get("roi_frame_width"), cfg.get("roi_frame_height"))
        _roi_to_wh = (width, height)

        def _scaled_roi(key: str):
            polys = scale_polygons(cfg.get(key) or [], _roi_from_wh, _roi_to_wh)
            return polys or None

        self.events_detector = BehaviorEventDetector(
            loiter_sec=cfg.get("loiter_sec", 120.0),
            loiter_radius_px=cfg.get("loiter_radius_px", 80.0),
            intrusion_roi=_scaled_roi("intrusion_roi"),
            loiter_roi=_scaled_roi("loiter_roi"),
            commercial_zone=cfg.get("commercial_zone", False),
            block_id=block_id, node_id=node_id)
        # 분석 영역. 지정하지 않으면 None 이고 화면 전체를 쓴다.
        self.analysis_roi = _scaled_roi("analysis_roi")

        from .semantic_risk_agent import RISK_TAXONOMY, SemanticRiskAgent
        self._taxonomy = RISK_TAXONOMY
        self.risk_agent = SemanticRiskAgent(
            density_hi=cfg.get("alert_density", 40.0))

        # 위험물 투기(배경차분) — 2026-08-29 신설. detector 모드에서 실제
        # 프레임을 처음 받을 때 지연 생성한다(cv2 배경차분기 준비 비용을
        # mock 전용 카메라에서는 안 쓰게). ``False``면 생성이 한 번
        # 실패했다는 뜻 — 매 틱 재시도하지 않는다.
        self.abandoned_detector: Any = None

        self._recent_events: list[BehaviorEvent] = []

    def switch_sources(self, person: str | None = None, environment: str | None = None,
                       stereo: str | None = None) -> dict:
        """실행 중 소스를 교체한다(대시보드 UI용).

        ⚠️ **런타임 전용 — 설정 파일에 저장하지 않는다.** 서버를 재시작하면
        ``blocks.json``의 값으로 돌아간다. 대시보드에 인증이 없는 상태라
        (docs/solution_productization_plan.md P0-2), 웹 요청으로 설정 파일을
        덮어쓰게 만들지 않으려는 의도적 선택이다. 영구 변경은 파일을 직접 수정.

        교체 실패 시 기존 소스를 유지하고 사유를 함께 반환한다.
        """
        errors = []

        if person and person != self.source_type:
            if person == "detector":
                try:
                    src_cfg = self.cfg.get("source") or {}
                    grid = src_cfg.get("tile_grid", [2, 2])
                    self.source = DetectorCrowdSource(
                        arch=src_cfg.get("arch", "fasterrcnn"),
                        conf=src_cfg.get("conf", 0.4),
                        tile_grid=tuple(grid) if grid else None,
                        nms_iou=src_cfg.get("nms_iou", 0.4))
                    self.source_type = "detector"
                except Exception as e:  # noqa: BLE001
                    errors.append(f"detector 로드 실패: {str(e)[:100]}")
            else:
                self.source = MockCrowdSource(width=self.w, height=self.h)
                self.source_type = "mock"

        # device 전환은 URL이 설정돼 있어야 한다. 없으면 build_*가 조용히 mock으로
        # 되돌리므로, 사용자가 "눌렀는데 아무 일도 없다"고 느끼지 않도록 사유를 알린다.
        if environment:
            base = dict(self.cfg.get("environment") or {})
            base["type"] = environment
            if environment == "device" and not base.get("url"):
                errors.append("환경센서: 장비 URL 미설정 — configs/blocks.json의 "
                              "crowd.environment.url을 지정하세요")
            else:
                self.env_provider = build_environment(base)

        if stereo:
            base = dict(self.cfg.get("stereo") or {})
            base["type"] = stereo
            if stereo == "device" and not base.get("url"):
                errors.append("스테레오: 장비 URL 미설정 — configs/blocks.json의 "
                              "crowd.stereo.url을 지정하세요")
            else:
                self.depth_provider = build_depth(base)

        return {"ok": not errors, "errors": errors, **self.source_status()}

    def source_status(self) -> dict:
        """현재 소스 구성(대시보드 표시용)."""
        return {
            "person_source": self.source_type,
            "environment": type(self.env_provider).__name__,
            "stereo": type(self.depth_provider).__name__,
            "environment_mode": ("device" if "Device" in type(self.env_provider).__name__
                                 else "mock"),
            "stereo_mode": ("device" if "Device" in type(self.depth_provider).__name__
                            else "mock"),
        }

    def step(self, t_sec: float, frame_bgr=None) -> CrowdSnapshot:
        """한 틱 분석. ``frame_bgr``는 detector 모드에서만 사용."""
        env = self.env_provider.at(t_sec)

        # 1) 사람 박스 확보
        #
        # ⚠️ 검출기 소스는 **프레임이 있어야만** 사람을 찾을 수 있다
        # (``boxes_at`` 이 없다). 예전에는 프레임 없이 불리면 그대로
        # ``AttributeError`` 로 터져 **`/api/crowd/live` 가 500** 을 냈다
        # (오류 관리 S-92 에서 12회 잡혀 발견). 화면이 주기적으로 부르는
        # 경로라 조용히 반복해서 실패하고 있었다.
        #
        # 프레임이 없으면 「관측하지 못했다」이지 오류가 아니다. 빈 결과로
        # 진행해 나머지 지표(환경·추적 상태)는 정상적으로 내려 준다.
        if self.source_type == "detector":
            if frame_bgr is None:
                boxes = np.zeros((0, 4), dtype=float)
                scores = np.zeros((0,), dtype=float)
            else:
                boxes, scores = self.source.boxes_from_frame(frame_bgr)
        else:
            boxes, scores, _ = self.source.boxes_at(t_sec)

        # 2) 추적 (ByteTrack) -- 실제 신뢰도를 넘겨 2단계 매칭이 동작하도록.
        #    기존 sam3_pipeline은 더미 np.ones()를 넣어 이 기능이 무력화돼 있었다.
        behavior, det = self.tracker.update(boxes, scores, t_sec)

        tracked_boxes = getattr(det, "xyxy", np.zeros((0, 4)))
        tracked_ids = getattr(det, "tracker_id", None)
        if tracked_ids is None:
            tracked_ids = np.zeros(len(tracked_boxes), int)
        # 위험물 투기 판정용 — 분석 영역 밖(예: 차도)에 서 있는 사람도
        # 배경차분 마스크에서는 제외해야 하므로, 영역으로 거르기 전
        # 전체 사람 상자를 따로 남겨 둔다.
        all_person_boxes = tracked_boxes

        # 2-1) 분석 영역으로 거르기.
        # 교통 CCTV는 도로를 향해 있어 화면 전체를 재면 차도까지 분모에 들어간다.
        # 보행 구역만 지정하면 인원·밀집도가 실제와 가까워진다. 추적은 화면
        # 전체에서 계속한다 — 영역 안팎을 드나드는 사람의 ID가 끊기면 배회·침입
        # 판정이 무너지기 때문이다.
        if self.analysis_roi is not None and len(tracked_boxes):
            keep = np.array([point_in_polygons(
                ((float(b[0]) + float(b[2])) * 0.5, float(b[3])), self.analysis_roi)
                for b in tracked_boxes], bool)
            tracked_boxes = tracked_boxes[keep]
            tracked_ids = np.asarray(tracked_ids)[keep]

        # 3) 배회/침입/쓰러짐
        new_events = self.events_detector.update(t_sec, tracked_boxes, tracked_ids, env=env)
        if new_events:
            self._recent_events = (new_events + self._recent_events)[:20]

        # 3-1) 위험물 투기 — 배경차분(고전 CV, GPU 불필요). detector
        # 모드에서 **실제 프레임**이 있을 때만 돈다 — mock 모드는 사람
        # 위치가 화면과 무관해 제외 영역이 잘못될 수 있다(모듈 docstring
        # 참고). 실패해도 나머지 분석은 계속돼야 하므로 예외를 삼킨다.
        if self.source_type == "detector" and frame_bgr is not None:
            if self.abandoned_detector is None:
                try:
                    from .abandoned_object import AbandonedObjectDetector
                    self.abandoned_detector = AbandonedObjectDetector(
                        block_id=self.block_id, node_id=self.node_id)
                except Exception:  # noqa: BLE001
                    self.abandoned_detector = False  # 재시도 안 함
            if self.abandoned_detector:
                try:
                    obj_events = self.abandoned_detector.update(
                        t_sec, frame_bgr, all_person_boxes)
                except Exception:  # noqa: BLE001
                    obj_events = []
                if obj_events:
                    new_events = new_events + obj_events
                    self._recent_events = (obj_events + self._recent_events)[:20]

        # 4) 밀집도 -- LiDAR 제외로 영상 기반 격자 밀집도만 사용
        grid, _ = grid_density(tracked_boxes, self.w, self.h)
        density_index = float(grid.max()) if grid.size else 0.0
        density_pct = float((grid > 0.05).mean() * 100) if grid.size else 0.0

        depth = self.depth_provider.at(t_sec, tracked_boxes)

        # 5) 위험도 -- 기존 SemanticRiskAgent 재사용
        assessment = self.risk_agent.assess(
            {"density_pct": density_pct}, behavior, vlm_text="")
        code = assessment.get("risk_code", "NORMAL")
        name, severity = self._taxonomy.get(code, ("정상", 0))

        # 배회/침입은 별도 축이므로 심각도를 최소 1 이상으로 올린다
        if new_events and severity < 1:
            severity = 1

        return CrowdSnapshot(
            t_sec=t_sec, person_count=int(len(tracked_boxes)),
            density_index=density_index,
            mean_speed=float(behavior.get("mean_speed", 0.0)),
            trajectory_variance=float(behavior.get("dispersion", 0.0)),
            action_label=_ACTION_BY_RISK.get(code, code),
            risk_code=code, risk_name=name,
            # ⚠️ 판정기는 키 이름이 ``score`` 다. 여기서 ``risk_score`` 를
            # 찾고 있어 **위험도 점수가 항상 0.0** 이었다. 등급(risk_code·
            # severity)은 제 키를 써서 정상이라 눈에 띄지 않았다.
            risk_score=float(assessment.get("score",
                                            assessment.get("risk_score", 0.0))),
            severity=int(severity),
            # 판정에 쓰인 값과 근거를 그대로 넘긴다 — 화면이 「왜」에 답해야 한다.
            surge=float(behavior.get("surge", 1.0)),
            divergence=float(behavior.get("divergence", 0.0)),
            drivers=list(assessment.get("drivers", []) or []),
            events=list(self._recent_events[:10]),
            env=env, depth=depth, source=self.source_type)
