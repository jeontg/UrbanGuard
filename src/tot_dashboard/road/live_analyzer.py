"""도로 노면 손상 탐지 — 기존 영상 / 실시간 CCTV 이중 모드.

## 두 가지 모드 (관리자 선택)

  - ``video`` : **기존 영상** 분석. 확보해 둔 영상 파일을 프레임 단위로 훑는다.
                재현 가능해 검증·시연에 적합하다.
  - ``cctv``  : **실시간 CCTV** 분석. ``configs/blocks.json``의 HLS 스트림에서
                프레임을 받아 분석한다.

``crowd/live_analyzer.py``·``crowd/field_sensors.py``와 동일한 provider 전환
패턴을 따른다 — 상위 로직(등급 판정·집계)은 어느 소스인지 모른다.

## ⚠️ 현재 탐지 성능에 대한 정직한 고지

이 모듈이 사용하는 모델(``data/datasets/rdd2022_czech/runs/road_defect_proto_v2``)은
**실측 결과 탐지 0건**이다:

  - 확보한 포트홀 영상 10종: conf 0.01까지 낮춰도 **0건**
  - 부산 CCTV 3개 블록: conf 0.01까지 낮춰도 **0건**

원인은 학습 데이터(RDD2022, 차량 블랙박스 근접 촬영)와 대상 영상의 **도메인 갭**
으로 확인됐다(docs/road_surface_management_plan.md Phase 2). 즉 **이 모듈은
지금 "정상 동작하지만 아무것도 못 찾는" 상태**이며, 화면에 0건이 뜨는 것은
버그가 아니라 모델의 한계다.

그럼에도 이 골격을 먼저 만드는 이유: 쓸 만한 모델이 확보되면 **경로 설정만
바꿔 바로 연결**할 수 있고, 그때까지 파이프라인·UI를 검증해 둘 수 있다.

## ⚠️ 라이선스

``road/defect_detection.py``가 Ultralytics(**AGPL-3.0**)를 쓴다. 침수 도메인은
torchvision(BSD)으로 전환했으나 도로 도메인은 아직이다 —
docs/yolo_license_alternatives.md 참고. 상용 납품 전 정리 필요.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2

from ..common.blocks import find_block, load_blocks, stream_url

log = logging.getLogger("urbanguard.road")
from ..common.config import PROJECT_ROOT
from ..common.video_io import VIDEO_EXTS, crop_letterbox

DEFAULT_MODEL = ("data/datasets/rdd2022_czech/runs/road_defect_proto_v2/weights/best.pt")
VIDEO_DIRS = [
    PROJECT_ROOT / "data" / "datasets" / "pothole_video_mendeley" / "samples",
    PROJECT_ROOT / "data" / "samples",
]

# 사양(docs/road_surface_management_plan.md 3-2절)의 4단계 등급
GRADE_NAME = {1: "정상", 2: "관찰", 3: "보수 필요", 4: "긴급 보수"}

# 실시간 CCTV 분석 창. 프레임 몇 장만 순간적으로 받으면 그때 지나가던 차량·
# 그림자에 결과가 좌우되므로, 일정 시간 관측하며 나눠 표본을 뽑는다.
CCTV_DURATION_SEC = 15.0
CCTV_SAMPLE_INTERVAL_SEC = 1.0      # 15초 창에서 약 15장


def list_available_videos() -> list[dict[str, Any]]:
    """분석 가능한 기존 영상 목록(관리자 선택용)."""
    out = []
    for d in VIDEO_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                cap = cv2.VideoCapture(str(p))
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                fps = cap.get(cv2.CAP_PROP_FPS) or 0
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                cap.release()
            except Exception:  # noqa: BLE001
                continue
            out.append({
                "id": str(p.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "name": p.name,
                "frames": n,
                "duration_sec": round(n / fps, 1) if fps else None,
                "resolution": f"{w}x{h}",
            })
    return out


@dataclass
class RoadAnalysisResult:
    """1회 분석 결과."""
    mode: str                                   # video | cctv
    target: str                                 # 영상 경로 또는 블록 id
    target_name: str
    frames_analyzed: int
    defects: list[dict] = field(default_factory=list)
    grade: int = 1
    grade_label: str = "정상"
    model_path: str = ""
    conf: float = 0.25
    elapsed_sec: float = 0.0
    note: str = ""
    # ★ S-88 증거 팝업이 「이벤트가 발생한 부분」에 쓴다(2026-08-20).
    #   **DB·API 에 직렬화하지 않는다** — 원본 프레임(numpy 배열)이라 JSON 이
    #   못 되고, 애초에 이 값의 용도는 그 자리에서 증거 링 버퍼로 넘기는
    #   것뿐이다(``continuous.py`` 의 ``_analyze_once`` 참고). `to_dict()`
    #   가 이 필드를 넣지 않으므로 안전하다.
    evidence_frame: Any = field(default=None, repr=False, compare=False)
    # 그 프레임에서 실제로 찾은 손상만(다른 프레임의 손상과 섞이지 않는다).
    evidence_boxes: list[dict] = field(default_factory=list,
                                       repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "target": self.target, "target_name": self.target_name,
            "frames_analyzed": self.frames_analyzed,
            "defect_count": len(self.defects), "defects": self.defects,
            "grade": self.grade, "grade_label": self.grade_label,
            "model_path": self.model_path, "conf": self.conf,
            "elapsed_sec": round(self.elapsed_sec, 1), "note": self.note,
            "mock": False,      # 이 경로는 실제 모델 추론 결과(0건이어도 실측)
        }


class RoadDefectAnalyzer:
    """기존 영상 / 실시간 CCTV 노면 손상 분석기.

    모델 로드는 **최초 분석 요청 시 지연 로드**한다 — 대시보드 기동 시간을
    늘리지 않고, 모델 파일이 없어도 서비스는 정상적으로 뜨게 하기 위함이다.
    """

    def __init__(self, model_path: str | None = None, conf: float = 0.25):
        self.model_path = model_path or DEFAULT_MODEL
        self.conf = conf
        self._model = None
        self._load_error: str | None = None
        self._lock = threading.Lock()
        self.last_result: RoadAnalysisResult | None = None

    # -- 모델 -------------------------------------------------------
    def _ensure_model(self):
        if self._model is not None or self._load_error:
            return self._model
        path = PROJECT_ROOT / self.model_path
        if not path.exists():
            self._load_error = f"모델 파일 없음: {self.model_path}"
            return None
        try:
            # **공통 로더를 쓴다.** ONNX·OpenVINO 후보를 골랐을 때
            #   * task 를 명시하지 않으면 분할 모델이 검출로 열리고
            #   * 장치를 안 정하면 내장 GPU 커널 컴파일을 시도하며
            #   * 예열이 없으면 첫 십수 회가 10배 느리다
            # 셋 다 조용히 벌어져서 화면만 봐서는 모른다.
            from ..core import inference
            self._model = inference.load(path, task=self._task())
        except Exception as e:  # noqa: BLE001
            self._load_error = f"모델 로드 실패: {str(e)[:120]}"
        return self._model

    def _task(self) -> str:
        """레지스트리가 아는 모델이면 거기서, 아니면 검출로 본다.

        노면은 손상 상자를 세는 일이라 기본은 검출이다. 다만 분할 모델을
        후보로 올려 두고 고를 수 있으므로, 아는 모델이면 등록된 백엔드를
        따른다.
        """
        try:
            from ..core import inference
            from ..core import model_registry as registry
            info = registry.get(self.model_path)
            if info is not None:
                return inference.task_of(info.backend)
        except Exception:  # noqa: BLE001
            pass
        return "detect"

    def set_model(self, model_path: str) -> bool:
        """쓰는 모델을 바꾼다. 실제로 바뀌었으면 True.

        재기동 없이 바꿀 수 있어야 하는 이유 — 상시 순회는 이 객체 하나를
        계속 쓰고 있어서, 교체하려고 서비스를 내리면 **그 사이 관측이 통째로
        비는** 시간이 생긴다. 다음 분석부터 새 모델이 지연 로드된다.

        분석 중에 갈아 끼우면 그 회차의 결과가 두 모델에 걸쳐 나온다.
        분석과 같은 잠금을 쓰므로 진행 중인 분석이 끝난 뒤에 바뀐다.
        """
        model_path = (model_path or "").strip()
        if not model_path or model_path == self.model_path:
            return False
        with self._lock:
            self.model_path = model_path
            self._model = None          # 다음 분석에서 새로 로드한다
            self._load_error = None
        log.info("노면 모델 교체: %s", model_path)
        return True

    def model_status(self) -> dict[str, Any]:
        return {
            "model_path": self.model_path,
            "exists": (PROJECT_ROOT / self.model_path).exists(),
            "loaded": self._model is not None,
            "error": self._load_error,
        }

    @staticmethod
    def _pick_evidence(frames: list, defects: list[dict]):
        """증거로 남길 대표 프레임과 **그 프레임의** 손상 상자만 고른다.

        ★ S-88 증거 팝업이 「이벤트가 발생한 부분」에 쓴다(2026-08-20).

        ⚠️ **다른 프레임의 손상과 섞지 않는다.** 15초 동안 여러 프레임을
        보는데, 손상이 여러 프레임에 걸쳐 있으면 상자와 화면이 서로 다른
        순간의 것이 되어 「여기서 났다」가 거짓말이 된다. 그래서 **손상이
        가장 많이 잡힌 프레임 하나만** 고르고, 그 프레임의 상자만 쓴다.

        손상이 하나도 없으면 ``(None, [])`` — 지어내지 않는다.
        """
        if not defects or not frames:
            return None, []
        by_frame: dict[int, list[dict]] = {}
        for d in defects:
            by_frame.setdefault(d["frame"], []).append(d)
        best_fno = max(by_frame, key=lambda fno: len(by_frame[fno]))
        frame = next((fr for fno, fr in frames if fno == best_fno), None)
        if frame is None:
            return None, []
        boxes = [{"x1": d["box"][0], "y1": d["box"][1],
                 "x2": d["box"][2], "y2": d["box"][3], "label": d["type"]}
                for d in by_frame[best_fno]]
        return frame, boxes

    # -- 공통 프레임 분석 -------------------------------------------
    def _analyze_frames(self, frames: list, conf: float,
                        roi: dict | None = None) -> list[dict]:
        """표본 프레임에서 노면 손상을 찾는다.

        ★ 2026-08-22 ``roi`` 추가 — S-81 에서 저장한 노면 ``analysis_roi``
        가 그동안 **저장만 되고 판정에는 전혀 쓰이지 않았다.** 화면은
        「설정하지 않으면 판정이 부정확합니다」라고 안내하는데 실제로는
        설정해도 달라지는 것이 없었다(전수점검에서 발견).

        ⚠️ **프레임을 마스킹·크롭하지 않는다.** 탐지는 항상 원본 전체
        프레임에 그대로 돌리고, **결과(박스 중심점)만 ROI 로 거른다** —
        픽셀을 검게 칠하면 그 인위적 경계를 모델이 균열·그림자로 오탐할
        위험이 있다(침수 도메인이 쓰는 「세그멘테이션은 전체, 지표만 ROI
        교차」 원칙과 같다).

        ⚠️ 박스가 ROI 경계에 걸치면 **중심점 기준 이진 판정**이라 통째로
        빠질 수 있다. 1차는 단순성을 택했고, 실사용에서 경계 누락이
        문제로 확인되면 겹침비율 방식으로 개선한다(2차 과제).

        ``roi`` 가 없거나 ``analysis_roi`` 가 비어 있으면 **예전과 똑같이**
        전체 프레임 결과를 그대로 돌려준다(``analysis_roi`` 는
        ``required=False`` 라 미설정 카메라가 많다 — 이 폴백이 이번 변경의
        가장 중요한 회귀 방지 지점이다).
        """
        from ..common.roi import point_in_polygons, scale_polygons
        from .defect_detection import detect_defects
        model = self._ensure_model()
        if model is None:
            return []
        polys = ((roi or {}).get("analysis_roi")) or []
        roi_wh = ((roi or {}).get("roi_frame_width"),
                  (roi or {}).get("roi_frame_height"))
        out = []
        scaled: list | None = None
        for idx, (fno, frame) in enumerate(frames):
            r = detect_defects(model, frame, conf=conf)
            if polys and scaled is None:
                h, w = frame.shape[:2]
                scaled = scale_polygons(polys, roi_wh, (w, h))
            for d in r.defects:
                box = [int(v) for v in d.box]
                if scaled:
                    cx = (box[0] + box[2]) / 2.0
                    cy = (box[1] + box[3]) / 2.0
                    if not point_in_polygons((cx, cy), scaled):
                        continue
                out.append({
                    "frame": fno, "type": d.cls_name,
                    "confidence": round(d.confidence, 3),
                    "box": box,
                })
        return out

    @staticmethod
    def _grade_from(defects: list[dict]) -> int:
        """탐지 결과 → 4단계 등급.

        ⚠️ **근거 없는 잠정값이다.** 개발사가 화면을 채우기 위해 정한 규칙이며,
        도로 유지관리 실무 기준에서 가져온 것이 아니다
        (docs/road_surface_management_plan.md 3-2절 — 예시안, 확정 아님).

        대조한 외부 기준 — 전부 **구간 단위 지수**다
            국외
              * **ASTM D6433** (Standard Practice for Roads and Parking Lots
                Pavement Condition Index Surveys) — PCI **0~100**.
                86~100 양호(일상 점검만), 40 이하 심각(재건축 검토),
                0~10 파손. 손상마다 **저·중·고 심각도**를 매긴다.
                아스팔트 19종·콘크리트 15종의 손상 목록을 규정한다.
            국내
              * 서울특별시 **SPI**(도로포장 유지관리 매뉴얼) — 균열률·
                소성변형·평탄성(IRI)으로 산출해 보수 공법과 우선순위를 정한다.
              * 관리주체별 지수 체계: 고속도로 HPCI · 국도 NHPCI ·
                시단위 MPCI · 지역도로 LrPCI
                (국토교통 연구 「도로관리 평가시스템 개선방안」)

        왜 그대로 옮길 수 없나
            그 지수들은 균열률·소성변형·평탄성을 **구간 길이(㎞·㎡) 단위**로
            측정해 산출한다. 우리는 CCTV **한 장면에서 개수만** 센다.
            평탄성·소성변형은 2차원 영상에서 잴 수도 없다. 단위가 달라
            환산되지 않는다.

            즉 이 등급은 **「몇 개 보였나」이지 「도로 상태가 어떤가」가 아니다.**

        지금 규칙 (전부 [자체] 판단)
            포트홀 3개 이상 → 4등급 / 1개 이상 → 3등급 / 균열만 → 2등급 /
            없음 → 1등급. **「3개」에 근거가 없다.**

        남은 과제
            ① 손상마다 **저·중·고 심각도**를 매긴다 (ASTM D6433 방식) —
               개수만 세는 지금 방식보다 실무 기준에 가깝다
            ② 구간 단위 집계로 바꾼다 — CCTV 한 지점이 담당하는 도로 구간
               길이를 알아야 ㎡당 손상 밀도를 낼 수 있다
            ③ 도로관리 부서의 SPI 운용 기준과 대응표를 만든다
        """
        if not defects:
            return 1
        n_pothole = sum(1 for d in defects if d["type"] == "pothole")
        if n_pothole >= 3:
            return 4
        if n_pothole >= 1:
            return 3
        return 2

    # -- 모드 1: 기존 영상 ------------------------------------------
    def analyze_video(self, video_id: str, max_frames: int = 12,
                      conf: float | None = None) -> RoadAnalysisResult:
        conf = self.conf if conf is None else conf
        t0 = time.time()
        path = PROJECT_ROOT / video_id
        if not path.exists() or path.suffix.lower() not in VIDEO_EXTS:
            return RoadAnalysisResult(mode="video", target=video_id, target_name=video_id,
                                      frames_analyzed=0, model_path=self.model_path,
                                      conf=conf, note=f"영상을 찾을 수 없습니다: {video_id}")
        cap = cv2.VideoCapture(str(path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        step = max(1, total // max(max_frames, 1))
        frames = []
        for i in range(0, total, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, fr = cap.read()
            if not ok:
                break
            # 레터박스가 있으면 잘라낸다 -- 검은 여백을 손상으로 오인하지 않도록
            frames.append((i, crop_letterbox(fr)))
            if len(frames) >= max_frames:
                break
        cap.release()

        defects = self._analyze_frames(frames, conf)
        grade = self._grade_from(defects)
        _ev_frame, _ev_boxes = self._pick_evidence(frames, defects)
        res = RoadAnalysisResult(
            mode="video", target=video_id, target_name=path.name,
            frames_analyzed=len(frames), defects=defects,
            grade=grade, grade_label=GRADE_NAME[grade],
            model_path=self.model_path, conf=conf, elapsed_sec=time.time() - t0,
            note=self._load_error or "",
            evidence_frame=_ev_frame, evidence_boxes=_ev_boxes)
        self.last_result = res
        return res

    # -- 모드 2: 실시간 CCTV ----------------------------------------
    def analyze_cctv(self, block_id: str, duration_sec: float = CCTV_DURATION_SEC,
                     sample_interval_sec: float = CCTV_SAMPLE_INTERVAL_SEC,
                     conf: float | None = None,
                     block: dict | None = None,
                     frame_sink=None) -> RoadAnalysisResult:
        """실시간 스트림을 ``duration_sec`` 동안 관측하며 주기적으로 표본 추출.

        프레임 몇 장만 즉시 받으면 그 순간의 차량·그림자에 결과가 좌우된다.
        일정 시간 창에서 나눠 뽑으면 순간적인 가림이 지나간 뒤의 노면도 포함돼
        더 안정적이다.

        ``block`` 을 넘기면 그 정보를 그대로 쓴다. CCTV는 이제 DB(S-80)에서
        관리하므로, 호출부가 DB에서 찾은 카메라를 넘겨 주는 것이 정상 경로다.
        생략하면 예전처럼 ``blocks.json`` 에서 찾는다 — DB에만 등록된 카메라는
        이 경로로는 찾을 수 없다.

        ⚠️ 벽시계 시간 기준으로 창을 관리한다 — HLS는 버퍼된 구간을 실시간보다
        빠르게 디코딩할 수 있어, 프레임 수만 세면 실제 경과 시간과 어긋난다.

        ``frame_sink`` 를 넘기면 뽑아 둔 프레임 목록을 그대로 건네준다. 학습
        데이터 수집(``road/dataset_collector.py``)이 쓰는 통로다 — 수집기가
        스트림을 따로 열면 같은 카메라에 세 번째로 붙는 셈이라 CCTV 서버가
        연결을 거절한다(실측).
        """
        conf = self.conf if conf is None else conf
        t0 = time.time()
        if block is None:
            block = find_block(block_id)
        if block is None:
            return RoadAnalysisResult(mode="cctv", target=block_id, target_name=block_id,
                                      frames_analyzed=0, model_path=self.model_path,
                                      conf=conf, note=f"블록을 찾을 수 없습니다: {block_id}")
        # 실시간 스트림이 우선. 없으면 **등록된 동영상 파일**을 본다 —
        # S-80에서 동영상을 올려 노면 대상으로 지정한 지점이 「스트림이 없다」로
        # 끝나면, 등록해 둔 자료를 쓸 방법이 없다.
        url = stream_url(block)
        if not url:
            src = block.get("source") or {}
            if src.get("type") == "video" and src.get("path"):
                path = Path(src["path"])
                if not path.is_absolute():
                    path = PROJECT_ROOT / path
                if path.is_file():
                    url = str(path)
        if not url:
            return RoadAnalysisResult(mode="cctv", target=block_id,
                                      target_name=block.get("name", block_id),
                                      frames_analyzed=0, model_path=self.model_path,
                                      conf=conf,
                                      note="이 지점은 실시간 스트림도 동영상 파일도 "
                                           "없습니다. CCTV 관리에서 소스를 확인하세요.")

        # 스트림은 매번 새로 열어 최신 프레임을 받는다(캐시된 오래된 프레임 방지).
        # ⚠️ 같은 카메라를 침수·인파가 이미 보고 있으면 CCTV 서버가 세 번째
        # 연결을 거절해 즉시 실패한다(실측). 한 번은 쉬었다 다시 붙어 본다.
        frames: list = []
        opened = False
        for attempt in (1, 2):
            cap = cv2.VideoCapture(url)
            opened = cap.isOpened()
            if opened:
                deadline = time.time() + max(duration_sec, 0.5)
                next_sample = 0.0
                loop_t0 = time.time()
                while time.time() < deadline:
                    ok, fr = cap.read()
                    if not ok or fr is None:
                        # 스트림이 끊기면 무한 대기하지 않고 짧게 쉬었다 재시도
                        time.sleep(0.05)
                        continue
                    elapsed = time.time() - loop_t0
                    if elapsed >= next_sample:
                        frames.append((len(frames), fr))
                        next_sample = elapsed + sample_interval_sec
            cap.release()
            if frames or attempt == 2:
                break
            time.sleep(2.0)

        if not frames:
            # ★ 2026-08-29 실사용 중 발견 — 재배포(MediaMTX)를 켠 뒤에는
            # 이 지점의 `url`이 원본이 아니라 `rtsp://127.0.0.1:8554/...`
            # (재배포 주소)다. 재배포 서버 자체가 죽어 있으면(예: 재부팅
            # 후 자동 복구 안 됨 — scripts/urbanguard-run.cmd 참고) 이
            # 연결도 당연히 실패하는데, 예전 메시지는 "동시접속 경쟁"만
            # 언급해 운영자가 엉뚱한 곳(다른 탐지서비스)을 의심하게
            # 만들었다. `origin_url`이 남아 있으면(재배포로 치환된
            # 주소라는 표식, to_block_dict() 참고) 재배포 서버 상태부터
            # 확인해 정확한 원인을 알려준다.
            is_restream_url = bool((block.get("source") or {}).get("origin_url"))
            if not opened and is_restream_url:
                try:
                    from ..core import restream as _restream
                    healthy = _restream.mediamtx_healthy()
                except Exception:  # noqa: BLE001
                    healthy = None
                if healthy is False:
                    why = ("재배포 서버(MediaMTX)가 응답하지 않습니다 — 이 지점만의 "
                          "문제가 아니라 재배포를 쓰는 모든 지점에 영향을 줍니다. "
                          "서비스 운영자에게 확인을 요청하세요.")
                else:
                    why = ("재배포 서버는 응답하지만 이 카메라 경로에 연결하지 "
                          "못했습니다 — 원본 CCTV 서버 쪽 문제이거나 아직 "
                          "연결이 준비되지 않았을 수 있습니다.")
            elif not opened:
                why = ("연결이 거부되었습니다. 같은 카메라를 다른 탐지서비스가 "
                       "상시로 보고 있으면 동시 접속이 막힐 수 있습니다.")
            else:
                why = "스트림에 붙었지만 화면을 받지 못했습니다."
            return RoadAnalysisResult(mode="cctv", target=block_id,
                                      target_name=block.get("name", block_id),
                                      frames_analyzed=0, model_path=self.model_path,
                                      conf=conf, elapsed_sec=time.time() - t0,
                                      note=why)

        # 학습 데이터 수집은 분석보다 **먼저** 넘긴다. 검출이 실패하더라도
        # 프레임 자체는 확보해야 한다 — 지금 모델이 0건인 것이 바로 자체
        # 데이터가 필요한 이유다.
        if frame_sink is not None:
            try:
                frame_sink(frames)
            except Exception:  # noqa: BLE001
                log.exception("학습 프레임 전달 실패 block=%s", block_id)

        # ROI 는 to_block_dict() 가 block["road"] 에 실어 준다 — 상시 순회
        # (RoadContinuousWatcher)·집중 감시(RoadFocusWatcher) 양쪽 다 이미
        # block 을 넘기고 있어 호출부는 고칠 것이 없다.
        defects = self._analyze_frames(frames, conf, roi=(block or {}).get("road"))
        grade = self._grade_from(defects)
        _ev_frame, _ev_boxes = self._pick_evidence(frames, defects)
        res = RoadAnalysisResult(
            mode="cctv", target=block_id, target_name=block.get("name", block_id),
            frames_analyzed=len(frames), defects=defects,
            grade=grade, grade_label=GRADE_NAME[grade],
            model_path=self.model_path, conf=conf, elapsed_sec=time.time() - t0,
            note=self._load_error or "",
            evidence_frame=_ev_frame, evidence_boxes=_ev_boxes)
        self.last_result = res
        return res

    # -- 통합 진입점 -------------------------------------------------
    def analyze(self, mode: str, target: str, conf: float | None = None,
                max_frames: int | None = None,
                duration_sec: float | None = None,
                block: dict | None = None,
                frame_sink=None) -> RoadAnalysisResult:
        """관리자가 선택한 모드로 분석을 수행한다.

        ``duration_sec``는 실시간 CCTV 모드에서만 쓰인다(기본 15초).
        영상 모드는 파일 전체를 ``max_frames``장으로 나눠 훑으므로 시간 개념이 없다.
        ``block`` 은 CCTV 모드에서만 쓰이며, DB에서 찾은 카메라를 넘기는 통로다.

        동시 요청이 겹치면 모델 추론이 뒤엉킬 수 있어 직렬화한다.
        """
        with self._lock:
            if mode == "cctv":
                return self.analyze_cctv(
                    target, duration_sec=duration_sec or CCTV_DURATION_SEC, conf=conf,
                    block=block, frame_sink=frame_sink)
            return self.analyze_video(target, max_frames=max_frames or 12, conf=conf)


def list_cctv_targets() -> list[dict[str, Any]]:
    """실시간 분석 가능한 블록 목록(관리자 선택용)."""
    out = []
    for b in load_blocks():
        if stream_url(b):
            out.append({"id": b["id"], "name": b["name"]})
    return out
