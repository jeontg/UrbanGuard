"""AI 모델 레지스트리 — 어떤 모델을 쓸 수 있는지 디스크에서 찾아 알려 준다 (S-61).

왜 필요한가
    모델 경로가 코드 여기저기에 문자열로 박혀 있었습니다
    (``road/live_analyzer.py`` 의 ``DEFAULT_MODEL``, ``flood/config.py`` 의
    ``water_model_path`` …). 그래서 **다른 모델로 바꿔 보려면 코드를 고쳐야**
    했고, 학습해 둔 모델이 여러 개 있어도 화면에서는 존재조차 알 수 없었습니다.

    실제로 노면은 4번 학습해 후보가 둘(RDD2022 v0.3, SVRDD 14에폭) 있는데,
    **둘을 같은 CCTV에 대 보려면 코드를 고쳐 서비스를 재기동하는 수밖에**
    없었습니다. 이 모듈은 그 비교를 화면에서 하게 만드는 첫 조각입니다.

무엇을 하지 않는가
    **운영 모델을 바꾸지 않습니다.** 여기서 고른 모델은 시험 탐지
    (:mod:`.model_probe`)에만 쓰입니다. 상시 탐지가 쓰는 모델을 화면에서
    갈아 끼우는 것은 워처 재구성이 얽혀 있어 별도 작업입니다 — 어설프게
    묶으면 「화면에서는 바꿨는데 실제로는 예전 모델이 돌고 있는」 상태가
    생깁니다. 그건 지금까지 걷어내 온 종류의 결함입니다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..common.config import PROJECT_ROOT
from . import roles

# 백엔드 — 추론 방식이 달라 섞으면 안 된다.
YOLO_DET = "yolo-det"          # 박스 검출 (노면 손상, 사람)
YOLO_SEG = "yolo-seg"          # 인스턴스 세그멘테이션 (수면)
TV_SEG = "torchvision-seg"     # 시맨틱 세그멘테이션 (수면, 자체 학습)

BACKEND_LABELS = {
    YOLO_DET: "YOLO 검출", YOLO_SEG: "YOLO 세그멘테이션",
    TV_SEG: "torchvision 세그멘테이션",
}

# 짧은 이름은 roles 가 단일 출처다 — 예전에는 여기에 따로 적어 두어
# roles.DOMAIN_LABELS 와 값이 달랐다(그쪽은 「침수·교통위험」, 여기는 「침수」).
DOMAIN_LABELS = dict(roles.DOMAIN_SHORT)


@dataclass(frozen=True)
class ModelInfo:
    key: str                    # 화면·API 가 주고받는 식별자 = 정규화한 경로
    domain: str                 # flood / crowd / road / ""(분류 미상)
    label: str
    path: str                   # PROJECT_ROOT 기준 상대경로(가능하면) 또는 절대경로
    backend: str
    exists: bool = False
    size_mb: float = 0.0
    modified: datetime | None = None
    builtin: bool = False       # 제품이 아는 모델인가
    guessed: bool = False       # 도메인·백엔드를 경로에서 추측했는가
    note: str = ""
    in_use: str = ""            # 지금 운영에서 쓰는 자리(있으면 그 설명)

    def to_dict(self) -> dict:
        return {
            "key": self.key, "domain": self.domain,
            "domain_label": DOMAIN_LABELS.get(self.domain, "분류 미상"),
            "label": self.label, "path": self.path,
            "backend": self.backend,
            "backend_label": BACKEND_LABELS.get(self.backend, self.backend),
            "exists": self.exists, "size_mb": round(self.size_mb, 1),
            "modified": self.modified.isoformat() if self.modified else None,
            "builtin": self.builtin, "guessed": self.guessed,
            "note": self.note, "in_use": self.in_use,
        }


# --- 제품이 아는 모델 --------------------------------------------------------
#
# ``in_use`` 는 **지금 서비스가 실제로 그 경로를 읽고 있다**는 뜻이다. 화면에서
# 「이걸 바꾸면 운영이 바뀐다」와 「시험용 후보일 뿐이다」를 구분해야 한다.
KNOWN: list[dict] = [
    {"path": "models/best.pt", "domain": "flood", "backend": YOLO_SEG,
     "label": "침수 수면 세그멘테이션 (YOLO11-seg, 자체 학습 v2.1)",
     "note": "val F1 0.9392 · 부산 실환경 미검증",
     "in_use": "침수 상시 탐지 (flood/config.py)"},
    {"path": "data/datasets/flood_water_own/runs/flood_lraspp_384_v2/best.pt",
     "domain": "flood", "backend": TV_SEG,
     "label": "침수 수면 세그멘테이션 (torchvision LRASPP 384, 자체 학습)",
     "note": "CPU 추론용 경량 모델",
     "in_use": "침수 파이프라인 torchvision 백엔드 (service/runner.py)"},
    {"path": "models/yolo11s.pt", "domain": "crowd", "backend": YOLO_DET,
     "label": "사람·차량 검출 (YOLO11s 사전학습)",
     "note": "인파 계수와 개인정보 마스킹에 함께 쓰인다",
     "in_use": "인파 검출 · 개인정보 마스킹 (core/image_mask.py)"},
    {"path": "data/datasets/rdd2022_czech/runs/road_defect_proto_v2/weights/best.pt",
     "domain": "road", "backend": YOLO_DET,
     "label": "노면 손상 탐지 (RDD2022 체코, v0.3)",
     "note": "⚠ 부산 CCTV(240p)에서 차선 도색을 균열로 읽는 오탐 확인 (2026-08-13)",
     "in_use": "노면 상시 탐지 (road/live_analyzer.py)"},
]


# --- 자동 탐색 ---------------------------------------------------------------
#
# 학습을 돌리면 결과가 ``runs/<이름>/weights/best.pt`` 로 떨어진다. 새로 학습할
# 때마다 KNOWN 에 손으로 추가하게 하면 결국 아무도 추가하지 않는다.

def _scan_roots() -> list[Path]:
    """모델을 찾아볼 폴더들.

    보관소(``dev-PoC_DATA``)를 프로젝트 **옆 폴더**로 기본 포함한다. 학습
    데이터와 체크포인트를 프로젝트 밖으로 옮겨 두었기 때문인데, 그것 때문에
    학습해 둔 모델이 화면에서 사라지면 옮긴 의미가 없다.
    환경변수 ``URBANGUARD_MODEL_DIRS`` 로 바꾸거나 더할 수 있다(``;`` 구분).
    """
    roots = [PROJECT_ROOT / "models", PROJECT_ROOT / "data" / "datasets"]
    # 보관소 관례: 작업 폴더 이름 뒤에 ``_DATA`` 를 붙인 형제 폴더.
    #   D:\dev-PoC\UrbanGuard  →  D:\dev-PoC_DATA
    # 실제로 학습 체크포인트를 그리로 옮겨 두었는데, 그 때문에 학습해 둔 모델이
    # 화면에서 사라지면 옮긴 의미가 없다.
    workspace = PROJECT_ROOT.parent
    for cand in (workspace.with_name(workspace.name + "_DATA"),
                 workspace / "dev-PoC_DATA"):
        if cand.is_dir():
            roots.append(cand)
    extra = os.environ.get("URBANGUARD_MODEL_DIRS", "")
    for part in extra.split(";"):
        part = part.strip()
        if part:
            roots.append(Path(part))
    return [r for r in roots if r.is_dir()]


# 경로 열쇠말 → 도메인. 위에서부터 먼저 맞는 것을 쓴다.
#
# ⚠️ 범용 YOLO 버전 이름(yolo11·yolo26·yolov8 등)은 **여기 안 넣는다**
#   (2026-08-22 전수점검에서 빼냄). 그 이름은 아키텍처 세대를 말할 뿐 도메인과
#   무관하다 — 그런데 crowd 열쇠말에 들어 있었던 탓에, 실측해 보니 등록된
#   `models/yolo11s.pt`·`yolo26n.pt`·`yolo26s.pt` 등 **범용 검출 모델 5개
#   전부가 무조건 crowd 로 확정**돼 버렸고, 그 결과 `for_domain('traffic')`
#   이 0건을 반환했다(교통에 쓸 만한 범용 검출기가 전부 crowd 로 못박혀
#   traffic 목록에 안 보임). 이 이름들을 빼면 그 모델들은 분류 미상
#   (``domain == ""``)이 되고, `for_domain()` 은 미상 모델을 **어느 도메인
#   요청에도 함께 보여주므로**(아래 docstring 참고) traffic 화면에서도
#   고를 수 있게 된다 — 실제로 어느 도메인에 쓸지는 관리자가 화면에서
#   고르는 것이지, 파일명만으로 못 박을 일이 아니다.
_DOMAIN_HINTS = [
    (("svrdd", "rdd2022", "road", "pothole", "defect"), "road"),
    (("flood", "water", "lraspp", "deeplab"), "flood"),
    # ★ 교통은 crowd 보다 **앞**에 둔다. crowd 열쇠말에 "vehicle" 류가 섞인
    #   파일명(예: 예전의 `yolov8_vehicle.pt`)이 있으면, 뒤에 두었을 때
    #   그 모델이 인파로 잘못 분류된다(위에서부터 먼저 맞는 것을 쓰므로).
    # ★ 2026-08-26 — 파일명 규칙 확정(Phase 7, docs/202608210801 9절
    #   확인사항 6번 해소). 낙하물·화재연기는 **별도 도메인이 아니라
    #   교통위험 안의 위험유형**이다(core/vocabulary.py의 traffic_debris·
    #   traffic_fire_smoke, parent="traffic") — 그래서 새 모델 파일도
    #   `traffic` 로 분류되는 이 규칙을 그대로 쓰면 된다. 학습
    #   산출물은 `train_traffic_incident_yolo.py` 관례상
    #   `traffic_incident_own/runs/<run>/weights/best.pt` 에 떨어지므로
    #   "incident"·"debris"·"fire"·"smoke" 열쇠말을 추가한다.
    (("traffic", "vehicle", "car", "congestion", "speed",
      "incident", "debris", "fire", "smoke"), "traffic"),
    (("crowd", "person", "people", "sam"), "crowd"),
]

_TV_HINTS = ("lraspp", "deeplab", "_tv", "tv_")
_SEG_HINTS = ("-seg", "_seg", "seg_")


def _guess_domain(path_text: str) -> str:
    low = path_text.lower()
    for words, dom in _DOMAIN_HINTS:
        if any(w in low for w in words):
            return dom
    return ""


def _guess_backend(path_text: str) -> str:
    low = path_text.lower()
    if any(w in low for w in _TV_HINTS):
        return TV_SEG
    if any(w in low for w in _SEG_HINTS):
        return YOLO_SEG
    return YOLO_DET


def _rel(p: Path) -> str:
    """가능하면 프로젝트 기준 상대경로로. 화면에 절대경로를 늘어놓지 않는다."""
    try:
        return p.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def resolve(key: str) -> Path:
    """레지스트리 키(=경로)를 실제 파일 경로로 바꾼다."""
    p = Path(key)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _info_from(path: Path, spec: dict | None = None) -> ModelInfo:
    key = _rel(path)
    exists = path.is_file()
    size = path.stat().st_size / (1024 * 1024) if exists else 0.0
    mtime = (datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
             if exists else None)
    if spec is not None:
        return ModelInfo(
            key=key, domain=spec["domain"], label=spec["label"], path=key,
            backend=spec["backend"], exists=exists, size_mb=size, modified=mtime,
            builtin=True, guessed=False, note=spec.get("note", ""),
            in_use=spec.get("in_use", ""))
    return ModelInfo(
        key=key, domain=_guess_domain(key), label=_auto_label(path),
        path=key, backend=_guess_backend(key), exists=exists, size_mb=size,
        modified=mtime, builtin=False, guessed=True,
        note="경로에서 자동 인식한 모델입니다. 분류와 추론 방식이 맞는지 확인하십시오.")


def _auto_label(path: Path) -> str:
    """자동 발견한 모델의 표시 이름.

    학습 결과는 모두 ``runs/<학습이름>/weights/best.pt`` 로 떨어지므로, 부모
    폴더 이름을 그대로 쓰면 **전부 「weights」** 가 되어 어느 것이 어느 학습인지
    구분되지 않는다(실제로 그렇게 나왔다). 한 단계 위의 학습 이름을 쓴다.
    """
    parent = path.parent
    name = parent.parent.name if parent.name == "weights" else parent.name
    if not name:
        name = path.stem
    # best 가 아닌 파일은 어느 체크포인트인지 함께 밝힌다.
    return name if path.stem == "best" else f"{name} ({path.stem})"


# --- 변환 산출물(ONNX · OpenVINO) -------------------------------------------
#
# 같은 가중치를 다른 런타임으로 내보낸 것이라 **성능은 크게 다르고 결과는 같다**
# (2026-08-17 실측: ONNX 2.0배 · OpenVINO 7.2배, IoU 1.0000).
# 목록에 안 보이면 화면에서 비교할 수가 없어 여기서 찾아 준다.
#
# ⚠️ 이것들은 **운영에 적용된 것이 아니다.** 원본 ``.pt`` 와 나란히 놓이므로
# 라벨과 note 에서 그 사실이 분명해야 한다 — 「화면에서는 바꿨는데 실제로는
# 예전 모델이 돌고 있는」 상태를 만들지 않기 위해서다.

# 변환 산출물을 찾을 폴더. 학습 결과 전체를 훑으면 목록이 두 배가 된다.
_CONVERTED_DIRS = ("candidates",)


def _converted_source(p: Path) -> Path:
    """변환 산출물에 대응하는 원본 ``.pt`` 경로(있을 법한 자리)."""
    from . import inference

    stem = p.name[: -len(inference.OPENVINO_SUFFIX)] if p.is_dir() else p.stem
    return p.parent.parent / f"{stem}.pt"


def _scan_converted() -> list[ModelInfo]:
    from . import inference

    out: list[ModelInfo] = []
    for root in _scan_roots():
        for sub in _CONVERTED_DIRS:
            folder = root / sub
            if not folder.is_dir():
                continue
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for p in entries:
                if not inference.is_model_path(p):
                    continue
                # ``.pt`` 도 받는다. 후보 폴더에 사람이 일부러 넣은 것은
                # 변환 산출물이든 새로 받은 가중치든 **똑같이 후보**이며,
                # 똑같이 「운영 미적용」으로 보여야 한다.
                out.append(_converted_info(p, inference.runtime_of(p)))
    return out


def _converted_info(path: Path, runtime: str) -> ModelInfo:
    """변환 산출물의 표시 정보.

    도메인·백엔드는 **원본 ``.pt`` 에서 물려받는다.** 파일명만 보고 추측하면
    ``best.onnx`` 가 분할인지 검출인지 알 수 없고, 그것을 틀리면 추론이
    조용히 어긋난다(:mod:`.inference` 참조).
    """
    from . import inference

    src = _converted_source(path)
    src_key = _rel(src)
    spec = next((s for s in KNOWN if s["path"] == src_key), None)
    # 원본이 실제로 있어야 「변환 산출물」이다. 없으면 새로 받아 둔 가중치다.
    converted = src.exists()

    if spec is not None:
        domain, backend, base = spec["domain"], spec["backend"], spec["label"]
        guessed = False
    elif converted:
        domain, backend = _guess_domain(src_key), _guess_backend(src_key)
        base = _auto_label(src)
        guessed = True
    else:
        own = _rel(path)
        domain, backend = _guess_domain(own), _guess_backend(own)
        base = path.stem or path.name
        guessed = True

    label = f"{base} — {inference.RUNTIME_LABELS.get(runtime, runtime)}"
    speed = inference.MEASURED_SPEEDUP.get(runtime)
    speed_txt = (f" · 실측 약 {speed:g}배 (2026-08-17, 결과 동일)"
                 if converted and speed and runtime != inference.PT else "")

    size = 0.0
    mtime = None
    try:
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            size /= 1024 * 1024
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        else:
            st = path.stat()
            size, mtime = st.st_size / (1024 * 1024), datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc)
    except OSError:
        pass

    return ModelInfo(
        key=_rel(path), domain=domain, label=label, path=_rel(path),
        backend=backend, exists=True, size_mb=size, modified=mtime,
        builtin=False, guessed=guessed,
        note=((f"{src_key} 를 변환한 후보입니다. " if converted
               else "후보 폴더에 놓아 둔 모델입니다. ")
              + f"**운영에 적용되어 있지 않습니다.**{speed_txt}"),
        in_use="")


def all_models(*, include_missing: bool = True) -> list[ModelInfo]:
    """알려진 모델 + 자동 탐색한 모델. 같은 파일은 한 번만 나온다."""
    out: dict[str, ModelInfo] = {}

    for spec in KNOWN:
        info = _info_from(resolve(spec["path"]), spec)
        if info.exists or include_missing:
            out[info.key] = info

    for root in _scan_roots():
        try:
            found = list(root.rglob("*.pt"))
        except OSError:
            continue
        for p in found:
            name = p.name.lower()
            # 후보 폴더는 :func:`_scan_converted` 가 전담한다. 여기서 먼저
            # 집어 가면 「운영 미적용」 표시가 붙지 않는다.
            if p.parent.name in _CONVERTED_DIRS:
                continue
            # 학습 중간 산출물은 후보가 아니다 — epoch12.pt 같은 것이 수십 개다.
            # ``models/`` 바로 아래 파일은 사람이 일부러 놓아 둔 것이라 모두 받는다.
            if name not in ("best.pt", "last.pt") and p.parent.name != "models":
                continue
            # best.pt 가 있으면 같은 폴더의 last.pt 는 감춘다. 둘은 같은 학습의
            # 「가장 좋았던 에폭」과 「마지막 에폭」이라, 나란히 놓으면 목록만
            # 두 배가 되고 고를 때 헷갈린다.
            if name == "last.pt" and (p.parent / "best.pt").is_file():
                continue
            key = _rel(p)
            if key in out:
                continue
            out[key] = _info_from(p)

    for info in _scan_converted():
        out.setdefault(info.key, info)

    # 도메인 → 이름 순. 분류 미상은 맨 뒤로 보내 목록을 어지럽히지 않는다.
    order = {d.value: i for i, d in enumerate(roles.Domain)} | {"": 9}
    return sorted(out.values(),
                  key=lambda m: (order.get(m.domain, 8), not m.builtin, m.key))


def for_domain(domain: str, *, include_missing: bool = False) -> list[ModelInfo]:
    """한 도메인에서 고를 수 있는 모델.

    분류 미상(``domain == ""``)도 함께 준다 — 자동 인식이 틀렸을 때 사용자가
    직접 골라 시험해 볼 수 있어야 한다. 화면은 그것을 구분해 표시한다.
    """
    rows = [m for m in all_models(include_missing=include_missing)
            if m.domain == domain or m.domain == ""]
    return [m for m in rows if m.exists or include_missing]


def get(key: str) -> ModelInfo | None:
    for m in all_models():
        if m.key == key:
            return m
    # 레지스트리에 없지만 파일은 있는 경우(방금 학습하거나 변환한 것)도 받아 준다.
    from . import inference

    p = resolve(key)
    if p.exists() and inference.is_model_path(p):
        rt = inference.runtime_of(p)
        return _info_from(p) if rt == inference.PT else _converted_info(p, rt)
    return None


def default_for(domain: str) -> ModelInfo | None:
    """그 도메인에서 지금 운영이 쓰는 모델. 화면의 기본 선택값이 된다."""
    for m in all_models():
        if m.domain == domain and m.in_use and m.exists:
            return m
    rows = for_domain(domain)
    return rows[0] if rows else None
