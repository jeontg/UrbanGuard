"""AI 모델 학습 실행 — 관리자 화면에서 4개 탐지 도메인 모델을 재학습한다.

## 배경

``docs/pending_tasks.md`` A-7(★★ 「재학습·MLOps — 성능 기록 표가 아예
없음」)이 지적한 공백과, 「4대 탐지 기능 기술 정리」(2026-08-25)가 확인한
격차 — 침수·도로 노면은 학습 스크립트가 있으나 손으로 터미널에서 돌려야
했고, 교통위험·인파관리는 자체 학습 스크립트 자체가 없었다 — 를 함께
메운다.

## 왜 서브프로세스인가 (스레드가 아니라)

학습은 CPU를 몇 시간 통째로 쓴다. 웹 서버 프로세스 안에서 돌리면(스레드든
뭐든) **관제 서비스의 실시간 탐지와 CPU를 다툰다.** 별도 프로세스로 띄우면
운영체제 스케줄러가 CPU를 나눠 쓰게 하고, 문제가 생겨도 학습 프로세스만
죽으면 되지 관제 서비스 전체가 같이 죽지 않는다.

## 왜 동시 1건만 허용하는가

GPU가 없다 — 전부 CPU 학습이다. 두 학습을 동시에 돌리면 스레드를 나눠
가지며 **둘 다 느려질 뿐 빨라지지 않는다.** 도메인이 달라도 예외를 두지
않는다.

## 재기동 후 상태 복구

이 모듈은 살아있는 프로세스 핸들(:class:`subprocess.Popen`)을 메모리에도
들고 있어(:data:`_PROCS`) 정확한 종료 코드를 알 수 있다. 서버가 재기동되면
이 메모리는 사라지므로, DB에는 ``pid`` 만 남는다 — 그때는
:func:`psutil.pid_exists` 로 살아있는지만 확인하고, 죽어 있으면 "서버
재기동으로 결과를 알 수 없다"고 **사실대로** 적는다(추측으로 성공·실패를
단정하지 않는다).
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil
from sqlalchemy.orm import Session

from ..common.config import PROJECT_ROOT
from ..common.data_archive import dataset_hint, resolve_dataset
from .models import TrainingRun, User

# 서버가 켜져 있는 동안만 유효한 실시간 핸들. 재기동되면 사라진다(모듈 설명 참고).
_PROCS: dict[int, subprocess.Popen] = {}

LOG_ROOT = PROJECT_ROOT / "data" / "training_runs" / "_logs"


@dataclass
class ParamField:
    key: str
    label: str
    type: str = "number"          # number | select
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list[tuple[str, str]] | None = None  # (value, label)
    help: str = ""


@dataclass
class DomainSpec:
    key: str
    label: str
    arch_label: str
    script: str                          # scripts/ 아래 파일명
    dataset_keys: list[str]              # common.data_archive의 데이터셋 키
    fields: list[ParamField]
    build_args: Callable[[dict, str], list[str]]  # (params, name) -> argv 꼬리
    output_root: Path
    metrics_csv: str = "results.csv"     # run 폴더 아래 지표 CSV 파일명
    metrics_reader: Callable[[Path], dict] = field(default=None)


def _flood_args(p: dict, name: str) -> list[str]:
    return ["--epochs", str(int(p.get("epochs", 40))),
           "--arch", str(p.get("arch", "lraspp")),
           "--size", str(int(p.get("size", 384))),
           "--batch", str(int(p.get("batch", 8))),
           "--name", name]


def _road_args(p: dict, name: str) -> list[str]:
    return ["--dataset", str(p.get("dataset", "rdd2022")),
           "--epochs", str(int(p.get("epochs", 15))),
           "--imgsz", str(int(p.get("imgsz", 416))),
           "--batch", str(int(p.get("batch", 16))),
           "--name", name]


def _traffic_args(p: dict, name: str) -> list[str]:
    return ["--epochs", str(int(p.get("epochs", 30))),
           "--imgsz", str(int(p.get("imgsz", 416))),
           "--batch", str(int(p.get("batch", 16))),
           "--name", name]


def _crowd_args(p: dict, name: str) -> list[str]:
    return ["--epochs", str(int(p.get("epochs", 20))),
           "--size", str(int(p.get("size", 640))),
           "--batch", str(int(p.get("batch", 2))),
           "--name", name]


def _read_csv_last_row(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(lines) < 2:
        return {}
    header = [h.strip() for h in lines[0].split(",")]
    last = [v.strip() for v in lines[-1].split(",")]
    return dict(zip(header, last))


def _flood_metrics(run_dir: Path) -> dict:
    row = _read_csv_last_row(run_dir / "results.csv")
    if not row:
        return {}
    return {"epoch": row.get("epoch"), "train_loss": row.get("train_loss"),
           "val_iou": row.get("val_iou"), "val_f1": row.get("val_f1")}


def _yolo_metrics(run_dir: Path) -> dict:
    # ultralytics YOLO는 project/name/results.csv 를 그 자리에 낸다.
    row = _read_csv_last_row(run_dir / "results.csv")
    if not row:
        return {}
    return {"epoch": row.get("epoch"),
           "precision": row.get("metrics/precision(B)"),
           "recall": row.get("metrics/recall(B)"),
           "mAP50": row.get("metrics/mAP50(B)"),
           "mAP50_95": row.get("metrics/mAP50-95(B)")}


def _crowd_metrics(run_dir: Path) -> dict:
    row = _read_csv_last_row(run_dir / "results.csv")
    if not row:
        return {}
    return {"epoch": row.get("epoch"), "train_loss": row.get("train_loss"),
           "precision": row.get("precision"), "recall": row.get("recall"),
           "f1": row.get("f1")}


DOMAINS: dict[str, DomainSpec] = {
    "flood": DomainSpec(
        key="flood", label="침수", arch_label="LR-ASPP MobileNetV3 (torchvision, BSD)",
        script="train_flood_water_cpu.py",
        dataset_keys=["flood_labeled"],
        fields=[
            ParamField("epochs", "에폭", default=40, min=1, max=200,
                      help="1 epoch ≈ 4.6분(1,570장 기준). 기본 40이면 약 3시간"),
            ParamField("arch", "모델 구조", type="select", default="lraspp",
                      choices=[("lraspp", "LR-ASPP (빠름, 권장)"),
                               ("deeplabv3", "DeepLabV3 (조금 더 정확·느림)")]),
            ParamField("size", "입력 크기", type="select", default=384,
                      choices=[("320", "320"), ("384", "384 (권장)"), ("512", "512")]),
            ParamField("batch", "배치 크기", default=8, min=1, max=64),
        ],
        build_args=_flood_args,
        output_root=PROJECT_ROOT / "data" / "datasets" / "flood_water_own" / "runs",
        metrics_reader=_flood_metrics,
    ),
    "traffic": DomainSpec(
        key="traffic", label="교통위험", arch_label="YOLO11s 검출 (Ultralytics, AGPL)",
        script="train_traffic_vehicle_yolo.py",
        dataset_keys=["traffic_vehicle_yolo"],
        fields=[
            ParamField("epochs", "에폭", default=30, min=1, max=300),
            ParamField("imgsz", "입력 해상도", type="select", default=416,
                      choices=[("320", "320"), ("416", "416 (권장)"), ("640", "640")]),
            ParamField("batch", "배치 크기", default=16, min=1, max=64),
        ],
        build_args=_traffic_args,
        output_root=PROJECT_ROOT / "data" / "training_runs" / "traffic",
        metrics_reader=_yolo_metrics,
    ),
    "crowd": DomainSpec(
        key="crowd", label="인파관리", arch_label="Faster R-CNN MobileNetV3 (torchvision, BSD)",
        script="train_crowd_person_frcnn.py",
        dataset_keys=["crowd_person_yolo"],
        fields=[
            ParamField("epochs", "에폭", default=20, min=1, max=200),
            ParamField("size", "입력 크기", type="select", default=640,
                      choices=[("512", "512"), ("640", "640 (권장)"), ("800", "800")]),
            ParamField("batch", "배치 크기", default=2, min=1, max=16,
                      help="검출 모델은 세그멘테이션보다 이미지당 메모리를 훨씬 많이 씁니다"),
        ],
        build_args=_crowd_args,
        output_root=PROJECT_ROOT / "data" / "training_runs" / "crowd",
        metrics_reader=_crowd_metrics,
    ),
    "road": DomainSpec(
        key="road", label="도로 노면", arch_label="YOLOv8n 검출 (Ultralytics, AGPL)",
        script="train_road_defect_yolo.py",
        dataset_keys=["road_rdd2022_yolo", "road_svrdd_yolo"],
        fields=[
            ParamField("dataset", "학습 데이터셋", type="select", default="rdd2022",
                      choices=[("rdd2022", "RDD2022 체코 (2,829장)"),
                               ("svrdd", "SVRDD 45° 부감 (도메인 갭 완화 시도용)")]),
            ParamField("epochs", "에폭", default=15, min=1, max=300),
            ParamField("imgsz", "입력 해상도", type="select", default=416,
                      choices=[("320", "320"), ("416", "416 (권장)"), ("640", "640")]),
            ParamField("batch", "배치 크기", default=16, min=1, max=64),
        ],
        build_args=_road_args,
        output_root=PROJECT_ROOT / "data" / "training_runs" / "road",
        metrics_reader=_yolo_metrics,
    ),
}

# ⚠️ 4개 도메인 전부 실시간 탐지가 이 학습 결과를 **자동으로 넘겨받지
#   않는다.** 학습이 끝나면 AI 모델 관리(S-84·S-61) 화면에서 새 체크포인트를
#   운영 모델로 고르는 절차가 별도로 필요하다 — 학습=운영 반영이 아니다.
AUTO_APPLY_NOTE = ("학습이 끝나도 자동으로 운영에 반영되지 않습니다. "
                  "「AI 모델 운영·설정」 화면에서 새 체크포인트를 "
                  "직접 선택해야 합니다.")


def dataset_status(domain: str) -> dict:
    """이 도메인의 학습 데이터가 준비됐는지. 여러 데이터셋 키가 있으면 하나라도 있으면 준비됨."""
    spec = DOMAINS[domain]
    found = []
    missing_hints = []
    for k in spec.dataset_keys:
        p = resolve_dataset(k)
        if p is not None:
            n = sum(1 for f in (p / "images").glob("*") if f.is_file()) \
                if (p / "images").is_dir() else None
            found.append({"key": k, "path": str(p), "count": n})
        else:
            missing_hints.append(dataset_hint(k))
    return {"ready": bool(found), "found": found, "missing_hints": missing_hints}


def _reconcile_running(db: Session) -> None:
    """DB상 'running'인데 실제로는 죽어 있는 실행을 정리한다."""
    for run in db.query(TrainingRun).filter(TrainingRun.status == "running").all():
        proc = _PROCS.get(run.id)
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                continue  # 아직 살아 있음
            run.status = "succeeded" if rc == 0 else "failed"
            run.error = "" if rc == 0 else f"종료 코드 {rc} (로그 참고)"
            run.finished_at = datetime.now(timezone.utc)
            _PROCS.pop(run.id, None)
            _finalize_metrics(run)
        else:
            # 서버가 재기동돼 핸들을 잃었다 — pid 로만 생사 확인 가능.
            alive = run.pid is not None and psutil.pid_exists(run.pid)
            if not alive:
                run.status = "stopped"
                run.error = "서버 재기동으로 실행 핸들을 잃어 정확한 종료 코드를 알 수 없습니다."
                run.finished_at = datetime.now(timezone.utc)
                _finalize_metrics(run)
    db.commit()


def _finalize_metrics(run: TrainingRun) -> None:
    spec = DOMAINS.get(run.domain)
    if spec is None or not run.output_dir:
        return
    try:
        run.metrics = spec.metrics_reader(Path(run.output_dir)) or run.metrics
    except Exception:  # noqa: BLE001
        pass


def active_run(db: Session) -> TrainingRun | None:
    _reconcile_running(db)
    return db.query(TrainingRun).filter(TrainingRun.status == "running").first()


def start(db: Session, domain: str, params: dict, user: User) -> tuple[TrainingRun | None, str]:
    """학습을 시작한다. 실패하면 (None, 사유)."""
    if domain not in DOMAINS:
        return None, f"알 수 없는 도메인: {domain}"
    spec = DOMAINS[domain]

    busy = active_run(db)
    if busy is not None:
        return None, (f"이미 다른 학습이 실행 중입니다({DOMAINS[busy.domain].label}, "
                      f"실행 #{busy.id}). CPU 전용 환경이라 동시에 두 학습을 "
                      "돌리면 둘 다 느려질 뿐입니다 — 끝난 뒤 다시 시도하십시오.")

    ds = dataset_status(domain)
    if not ds["ready"]:
        hint = " / ".join(ds["missing_hints"])
        return None, f"학습 데이터가 없습니다. 다음 위치에 준비하십시오: {hint}"

    run = TrainingRun(domain=domain, status="running", params=params,
                      started_by=user.id, started_by_name=getattr(user, "name", "") or "",
                      started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()  # id 확보

    name = f"{domain}_{run.id}"
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / f"{name}.log"
    argv = [sys.executable, str(PROJECT_ROOT / "scripts" / spec.script)] + \
        spec.build_args(params, name)

    log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            argv, cwd=str(PROJECT_ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
            env=_child_env(), creationflags=_windows_new_group_flag(),
        )
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log_fh.close()
        return None, f"학습 프로세스를 시작하지 못했습니다: {str(e)[:200]}"

    run.pid = proc.pid
    run.log_path = str(log_path)
    run.output_dir = str(spec.output_root / name)
    db.commit()
    _PROCS[run.id] = proc
    return run, ""


def _child_env() -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _windows_new_group_flag() -> int:
    import os
    return 0x00000200 if os.name == "nt" else 0  # CREATE_NEW_PROCESS_GROUP


def stop(db: Session, run_id: int) -> tuple[bool, str]:
    run = db.get(TrainingRun, run_id)
    if run is None:
        return False, "실행 기록이 없습니다."
    if run.status != "running":
        return False, "이미 끝난 실행입니다."
    proc = _PROCS.get(run_id)
    try:
        if proc is not None:
            proc.terminate()
        elif run.pid and psutil.pid_exists(run.pid):
            psutil.Process(run.pid).terminate()
    except Exception as e:  # noqa: BLE001
        return False, f"중지에 실패했습니다: {str(e)[:160]}"
    run.status = "stopped"
    run.error = "운영자가 중지했습니다."
    run.finished_at = datetime.now(timezone.utc)
    _finalize_metrics(run)
    db.commit()
    _PROCS.pop(run_id, None)
    return True, ""


def tail_log(run: TrainingRun, n: int = 200) -> str:
    p = Path(run.log_path) if run.log_path else None
    if not p or not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(lines[-n:])


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 침수·인파 스크립트는 "[train] epoch 1/15  loss ..." 식으로 완료된 에폭마다
# 한 줄만 찍는다. 도로·교통(ultralytics YOLO)은 진행 중인 배치마다 다시
# 그려 "  2/15   ...  416: 37% ────── 59/160 ..." 식으로 에폭 분수와 그
# 안의 배치 진행률을 함께 찍는다 — 두 형식을 각각 잡는다.
_EPOCH_RE = re.compile(r"epoch\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_YOLO_EPOCH_RE = re.compile(r"(?m)^\s*(\d+)/(\d+)\b.*?(\d+)%")


def progress_percent(run: TrainingRun) -> dict | None:
    """로그 꼬리에서 진행률을 뽑는다. 못 찾으면 None(추측해서 지어내지 않는다).

    다른 도메인 탭에 "간단한 진행률만" 보여주기 위한 것이다(2026-08-25,
    사용자 요청 — 다른 도메인 탭에는 전체 로그 대신 한 줄 요약만).
    """
    text = _ANSI_RE.sub("", tail_log(run, 80))
    if not text:
        return None
    m = list(_YOLO_EPOCH_RE.finditer(text))
    if m:
        epoch, total, batch_pct = (int(x) for x in m[-1].groups())
        if total > 0:
            pct = round(((epoch - 1) + batch_pct / 100) / total * 100)
            return {"epoch": epoch, "total_epochs": total,
                   "percent": max(0, min(100, pct))}
    m = list(_EPOCH_RE.finditer(text))
    if m:
        epoch, total = (int(x) for x in m[-1].groups())
        if total > 0:
            return {"epoch": epoch, "total_epochs": total,
                   "percent": max(0, min(100, round(epoch / total * 100)))}
    return None


def list_runs(db: Session, domain: str | None = None, limit: int = 20) -> list[TrainingRun]:
    _reconcile_running(db)
    q = db.query(TrainingRun)
    if domain:
        q = q.filter(TrainingRun.domain == domain)
    return q.order_by(TrainingRun.started_at.desc()).limit(limit).all()


def refresh(db: Session, run: TrainingRun) -> TrainingRun:
    """실행 중이면 진행 중 지표를 실시간으로 다시 읽는다(끝날 때까지 기다리지 않고)."""
    if run.status == "running" and run.output_dir:
        _finalize_metrics(run)
    return run
