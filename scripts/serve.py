"""UrbanGuard 서비스 감시 기동 — 비정상 종료 시 되살린다.

왜 필요한가 (2026-08-12 사고)
    부산 CCTV 호스트가 한동안 풀리지 않는 동안 스트림 재접속이 반복되면서
    프로세스가 **세그폴트(exit 139)** 로 죽었다. 밤사이 서비스가 내려가 있었고
    아무도 몰랐다 — 침수·인파 탐지까지 함께 멎은 상태였다.

    ``common/stream_guard.py`` 로 방아쇠(무한 재접속)는 없앴지만, **세그폴트는
    파이썬에서 잡을 수 없다.** 네이티브 계층에서 죽으면 예외조차 오지 않는다.
    그러므로 마지막 방어선은 **바깥에서 지켜보다 되살리는 것**뿐이다.

무엇을 하는가
    uvicorn 을 자식 프로세스로 띄우고 지켜본다.

    * **정상 종료**(0) 또는 **운영자의 Ctrl+C** → 그대로 끝낸다
    * **화면에서 요청한 재기동**(코드 42) → 백오프 없이 곧바로 다시 띄운다.
      운영자가 의도한 것이므로 실패 횟수에 넣지 않는다(S-01 재기동 버튼)
    * **비정상 종료**(세그폴트 등) → 잠시 기다렸다 다시 띄운다
    * 재기동이 잦으면 간격을 늘린다 — 설정 오류로 즉시 죽는 상황에서
      무한 재기동 루프에 빠지면 로그만 채우고 문제를 가린다

⚠️ 이것은 임시 방편이다
    운영 납품에서는 **Windows 서비스(NSSM 등)나 작업 스케줄러**로 등록해
    부팅 시 자동 기동·자동 복구가 되게 해야 한다. 이 스크립트는 그때까지의
    대안이며, 서버가 재부팅되면 이것도 함께 죽는다.

Usage:
    python scripts/serve.py                      # 127.0.0.1:8000
    python scripts/serve.py --port 8080 --host 0.0.0.0
    python scripts/serve.py --max-restarts 0     # 무제한 재기동
    python scripts/serve.py --torch-threads 4    # 자식의 PyTorch 등 스레드 상한
    python scripts/serve.py --priority above_normal  # platform-shell 전용
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
LOG_DIR = PROJECT_ROOT / "data" / "logs"

# 재기동 간격. 즉시 다시 띄우면 같은 이유로 곧바로 또 죽는 경우가 많다.
RESTART_DELAY_SEC = 5.0
RESTART_DELAY_MAX_SEC = 120.0
# 이 시간 이상 살아 있었으면 「정상 운영 중이었다」로 보고 간격을 되돌린다.
HEALTHY_UPTIME_SEC = 300.0

# S-01 화면의 재기동 버튼이 쓰는 종료 코드와 표시.
# 두 값의 원본은 core/service_control.py 이며, 이 스크립트는 **의존성 없이
# 단독 실행**돼야 해서(패키지가 깨져도 서비스를 띄워야 한다) 상수만 복제한다.
# 한쪽을 고치면 다른 쪽도 고쳐야 한다.
RESTART_EXIT_CODE = 42
SUPERVISED_ENV = "URBANGUARD_SUPERVISED"

# ⚠️ 2026-09-02 실측 발견(속도 개선 1단계) — 저장소 전체에서
# `torch.set_num_threads()`를 명시적으로 부르는 곳은 `crowd/live_analyzer.py`
# 단 한 곳뿐이었고, 침수·교통위험·노면관리는 아무 제한 없이 PyTorch
# 기본 동작대로 "가용 코어 전부"를 쓰려 든다. 5개 서비스(플랫폼-쉘+도메인
# 4개)가 전부 별도 프로세스인데 각자 이러면, 실제 연산량과 무관하게
# 스케줄링 경합(과다구독)만 늘어난다. 코어 수에 비례해 서비스당 상한을
# 자동으로 나눠 준다 — 배포 환경(코어 수)이 달라져도 하드코딩 없이 맞는다.
_DEFAULT_TORCH_THREADS = max(1, (os.cpu_count() or 4) // 4)

# ⚠️ 2026-09-02 신설(속도 개선 2단계, 이후 서비스별 값으로 재설계) —
# 관리자가 서버 운영(S-87) 화면에서 **서비스마다 다른** 자동값 재정의를
# 저장할 수 있다. 그 화면은 DB(`core/settings.py`) 기반인데 이 스크립트는
# (자기 머리말대로) DB 코드를 임포트할 수 없으므로, 값은
# `core/settings.py::set_service_thread_overrides()`가 함께 적어 두는
# `{서비스 키: 정수}` 순수 JSON 파일로 전달받는다. **이 경로는 그쪽 파일의
# 경로 계산과 반드시 같아야 한다** — RESTART_EXIT_CODE 와 같은 이유로
# 상수를 양쪽에 복제해 둔다.
_SERVICE_THREADS_FILE = PROJECT_ROOT / "data" / "config" / "perf.json"


def _service_key_from_label(label: str) -> str:
    """`--label`(예: ``serve-crowd``, 플랫폼-쉘은 기본값 ``serve``)에서
    `service/routes_services.py::SERVICES`가 쓰는 것과 같은 서비스 키
    (``main``·``crowd``·``road``·``flood``·``traffic``)를 뽑는다."""
    return label[len("serve-"):] if label.startswith("serve-") else "main"


def _read_configured_torch_threads(service_key: str) -> int | None:
    """관리자가 이 서비스에 대해 저장해 둔 스레드 상한을 읽는다. 파일이
    없거나 깨졌거나 이 서비스 항목이 없거나 값이 이상하면 **절대 예외를
    내지 않고** `None`을 돌려준다 — 이 스크립트는 무슨 일이 있어도
    서비스를 띄워야 한다."""
    try:
        data = json.loads(_SERVICE_THREADS_FILE.read_text(encoding="utf-8"))
        n = int(data.get(service_key))
        return n if n >= 1 else None
    except Exception:  # noqa: BLE001
        return None


def _use_utf8_console() -> None:
    """콘솔 출력을 UTF-8 로 맞춘다.

    ⚠️ 한국어 Windows 의 기본 콘솔 코드페이지는 **cp949** 라, 「—」 같은 문자를
    찍는 순간 ``UnicodeEncodeError`` 로 감시 프로세스가 죽는다. 서비스를
    지키려고 띄운 감시가 로그 한 줄 때문에 죽으면 아무 의미가 없다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _reason(code: int) -> str:
    """종료 코드를 운영자가 읽을 수 있는 말로 바꾼다.

    Windows 는 종료 코드를 **부호 없는 32비트**로 준다. 강제 종료(-1)가
    ``4294967295`` 로 찍히면 로그를 보는 사람이 무슨 일인지 알 수 없다.
    """
    if code > 0x7FFFFFFF:
        code -= 0x100000000
    if code in (139, -11):
        return "세그폴트(네이티브 계층 크래시)"
    if code in (-1, 1):
        return f"강제 종료 또는 기동 실패 (code {code})"
    if code in (-9, 137):
        return "강제 종료(SIGKILL)"
    return f"비정상 종료 (code {code})"


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, log_file) -> None:
    line = f"[{_stamp()}] {msg}"
    # 로그 출력이 감시를 멈추게 해서는 안 된다 — 무슨 일이 있어도 삼킨다.
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        try:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        log_file.write(line + "\n")
        log_file.flush()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="UrbanGuard 감시 기동")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--max-restarts", type=int, default=20,
                    help="연속 재기동 상한(0=무제한). 상한에 닿으면 멈추고 알린다")
    # ⚠️ 2026-08-31 — API 게이트웨이 Phase 1(인파관리 서비스 분리)로
    # 이 감시자가 platform-shell(main:app) 말고 다른 앱(예:
    # crowd_service:app)도 지켜봐야 하게 됐다. 기본값은 기존과 100%
    # 같아 하위호환이 깨지지 않는다.
    ap.add_argument("--app", default="tot_dashboard.service.main:app",
                    help="uvicorn ASGI 앱 대상 (module:attr). 기본값은 기존 platform-shell")
    # 서비스마다 로그 파일이 겹치면(둘 다 serve.log) 감시 기동 메시지가
    # 서로 뒤섞인다 — 라벨로 파일명을 나눈다.
    ap.add_argument("--label", default="serve",
                    help="이 감시자의 로그 파일 이름(<label>.log). 기본 'serve'")
    # 2026-09-02 신설 — 속도 개선 1단계 §①. 기본값을 None으로 둬(코어
    # 수 기반 상수를 바로 안 씀) CLI에서 명시적으로 준 값인지 구분한다 —
    # 안 주면 관리자 화면 설정(2단계) > 코어 수 자동값 순으로 내려간다.
    ap.add_argument("--torch-threads", type=int, default=None,
                    help="자식 프로세스의 PyTorch/NumPy/OpenCV 내부 스레드풀 "
                         "상한. 생략하면 관리자 화면 설정을, 그것도 없으면 "
                         f"코어 수 기반 자동값({_DEFAULT_TORCH_THREADS})을 쓴다")
    # 2026-09-02 신설 — 속도 개선 1단계 §③. 도메인 서비스는 절대 이 값을
    # 안 바꾼다(공평한 경쟁 유지) — platform-shell 전용 실행 스크립트에서만
    # above_normal 을 넘긴다.
    ap.add_argument("--priority", choices=("normal", "above_normal"), default="normal",
                    help="자식 프로세스의 OS 스케줄링 우선순위(기본 normal)")
    args = ap.parse_args()

    _use_utf8_console()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{args.label}.log"

    cmd = [sys.executable, "-m", "uvicorn", args.app,
           "--app-dir", str(SRC), "--host", args.host, "--port", str(args.port)]

    # 자식에게 「너를 지켜보는 사람이 있다」를 알린다. 이 표시가 없으면 S-01
    # 재기동 버튼이 스스로 잠긴다 — 되살릴 것이 없는데 내리면 그냥 종료다.
    #
    # ⚠️ 2026-08-29 실사용 중 발견 — 위 _use_utf8_console()는 **이
    # 감시(supervisor) 프로세스 자신**의 stdout/stderr만 UTF-8로 고친다.
    # 실제 탐지 파이프라인 코드(runner.py 등)가 도는 곳은 여기서
    # subprocess.Popen으로 새로 띄우는 uvicorn **자식 프로세스**이고,
    # 자식은 완전히 새 파이썬 인터프리터라 부모의 reconfigure()를 물려받지
    # 않는다 — 자기 자신의 PYTHONIOENCODING(미설정 시 시스템 코드페이지,
    # 한국어 Windows는 cp949)로 stdout을 연다. 그 결과 「—」・「⚠」 같은
    # cp949 밖 문자가 하나만 로그에 찍혀도 그 print() 호출이
    # UnicodeEncodeError로 죽는다 — 자식(=실제 서비스)이 통째로 죽는
    # 것이다. 감시 프로세스만 안 죽게 고쳐 두고 정작 지켜야 할 대상은
    # 그대로 뒀던 셈이다. 환경변수로 자식의 인코딩 자체를 UTF-8로 강제해
    # 근본적으로 막는다(개별 print() 호출마다 방어 코드를 넣는 것보다
    # 확실하다).
    # 2026-09-02 신설 — 속도 개선 1단계 §①. OMP_NUM_THREADS·MKL_NUM_THREADS·
    # OPENBLAS_NUM_THREADS 는 PyTorch·NumPy(MKL/OpenBLAS 빌드)·OpenCV가
    # 프로세스 시작 시점에 자기 내부 스레드풀 크기를 정할 때 읽는 표준
    # 환경변수라, 침수·교통위험·노면관리처럼 코드에서 스레드 수를 따로
    # 지정하지 않은 곳까지 코드 수정 없이 그대로 적용된다.
    # 우선순위: CLI 명시 인자 > 관리자 화면 설정(서비스별, 2단계) > 코어
    # 수 자동값. `--label`로 "나는 어느 서비스인가"를 알아내 그 몫만 찾는다.
    service_key = _service_key_from_label(args.label)
    resolved_torch_threads = (args.torch_threads if args.torch_threads is not None
                              else (_read_configured_torch_threads(service_key)
                                    or _DEFAULT_TORCH_THREADS))
    _threads = str(resolved_torch_threads)
    child_env = {**os.environ, SUPERVISED_ENV: "1", "PYTHONIOENCODING": "utf-8",
                "OMP_NUM_THREADS": _threads, "MKL_NUM_THREADS": _threads,
                "OPENBLAS_NUM_THREADS": _threads}

    # 2026-09-02 신설 — 속도 개선 1단계 §③. HIGH_PRIORITY_CLASS 는 다른
    # 프로그램·OS 자체를 과도하게 굶길 수 있어(실측 없이 지어내지 않고
    # 보수적으로) 그 한 단계 아래만 쓴다.
    _creationflags = (subprocess.ABOVE_NORMAL_PRIORITY_CLASS
                      if args.priority == "above_normal" else 0)

    delay = RESTART_DELAY_SEC
    restarts = 0
    child: subprocess.Popen | None = None
    stopping = False

    def _on_signal(signum, frame):  # noqa: ARG001
        """Ctrl+C 는 **운영자의 의도**다. 되살리지 않고 함께 내려간다."""
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            try:
                child.terminate()
            except Exception:  # noqa: BLE001
                pass

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (AttributeError, ValueError):
        pass          # 플랫폼에 따라 없을 수 있다

    with log_path.open("a", encoding="utf-8") as lf:
        _log(f"감시 기동 시작 — http://{args.host}:{args.port}  (로그 {log_path})", lf)
        while not stopping:
            started = time.monotonic()
            child = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=child_env,
                                     creationflags=_creationflags)
            code = child.wait()
            uptime = time.monotonic() - started

            if stopping or code == 0:
                _log(f"서비스가 정상 종료했습니다 (code {code}).", lf)
                return 0

            if code == RESTART_EXIT_CODE:
                # 운영자가 화면에서 누른 것이다. 기다릴 이유도, 실패로 셀
                # 이유도 없다 — 여기서 백오프를 걸면 「눌렀는데 한참 안 뜬다」가
                # 되고, 실패로 세면 몇 번 누르다 감시가 멈춰 버린다.
                _log(f"운영자 요청 재기동 — {uptime / 60:.1f}분 동작 후 "
                     "곧바로 다시 띄웁니다.", lf)
                delay = RESTART_DELAY_SEC
                restarts = 0
                continue

            why = _reason(code)
            _log(f"⚠ {why} · {uptime / 60:.1f}분 동작 후 죽었습니다.", lf)

            if uptime >= HEALTHY_UPTIME_SEC:
                # 오래 잘 돌다 죽었다면 일시적 원인이다. 처음 간격으로 되돌린다.
                delay = RESTART_DELAY_SEC
                restarts = 0
            restarts += 1
            if args.max_restarts and restarts > args.max_restarts:
                _log(f"✗ 연속 {restarts - 1}회 재기동해도 안정되지 않아 멈춥니다. "
                     "설정·의존성을 확인해 주세요.", lf)
                return 1

            _log(f"{delay:.0f}초 뒤 재기동합니다 (연속 {restarts}회째).", lf)
            time.sleep(delay)
            delay = min(delay * 2, RESTART_DELAY_MAX_SEC)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
