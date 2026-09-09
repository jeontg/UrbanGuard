"""시설물 제어 (S-11) — 진입차단시설·배수펌프장 등.

**탐지에서 제어로 넘어가는 지점이라 제품의 책임 구조가 달라진다.**
차단막이 잘못 내려가 사고가 나거나, 침수인데 안 내려가 인명피해가 나면
책임 소재가 문제된다(설계서 2-1절). 그래서 3단계로 나누고 **기본값은
「권고만」**이다.

| 모드 | 우리 역할 | 위험 |
|---|---|---|
| advise  | 담당자에게 차단 권고 알림만. 조작은 사람이 | 낮음 (기본값) |
| request | 제어 요청 송신, 최종 실행은 시설 측 승인 | 중간 |
| auto    | 조건 충족 시 자동 제어 | 높음 — 별도 계약·보험 필요 |
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import FacilityControl

log = logging.getLogger("urbanguard.facility")

# 제어 모드
MODE_ADVISE = "advise"
MODE_REQUEST = "request"
MODE_AUTO = "auto"
MODE_LABELS = {MODE_ADVISE: "권고만", MODE_REQUEST: "원격 요청", MODE_AUTO: "자동 제어"}
MODE_DESC = {
    MODE_ADVISE: "담당자에게 차단 권고 알림만 보냅니다. 실제 조작은 사람이 합니다.",
    MODE_REQUEST: "제어 요청을 시설 측에 보냅니다. 최종 실행은 시설 측이 승인합니다.",
    MODE_AUTO: "조건이 충족되면 자동으로 제어합니다. 별도 계약·보험이 필요합니다.",
}
MODES = (MODE_ADVISE, MODE_REQUEST, MODE_AUTO)
KEY_MODE = "facility_mode"

# 명령
CMD_STATUS = "status"
CMD_ADVISE = "advise"
CMD_REMOTE = "remote"
CMD_LABELS = {CMD_STATUS: "상태 조회", CMD_ADVISE: "차단 권고 발송",
              CMD_REMOTE: "원격 제어 요청"}

# 결과
RESULT_OK = "ok"
RESULT_NOT_LINKED = "not_linked"
RESULT_BLOCKED = "blocked"
RESULT_LABELS = {RESULT_OK: "성공", RESULT_NOT_LINKED: "미연계",
                 RESULT_BLOCKED: "모드 제한으로 차단"}

LINK_LABELS = {"none": "미연계", "ok": "정상", "error": "연동 오류"}
TYPE_LABELS = {"gate": "진입차단시설", "pump": "배수펌프장", "sign": "전광 표지판"}

_cache: list[dict] | None = None


def config_path() -> Path:
    from ..common.config import PROJECT_ROOT
    override = os.environ.get("URBANGUARD_FACILITIES_PATH")
    return Path(override) if override else PROJECT_ROOT / "configs" / "facilities.json"


def load(force: bool = False) -> list[dict]:
    """시설 목록. 파일이 없거나 깨져도 화면은 떠야 하므로 빈 목록으로 내려간다."""
    global _cache
    if _cache is not None and not force:
        return _cache
    path = config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _cache = [f for f in data.get("facilities", []) if f.get("id")]
    except Exception:  # noqa: BLE001
        log.exception("시설 설정을 읽지 못했습니다: %s", path)
        _cache = []
    return _cache


def invalidate() -> None:
    global _cache
    _cache = None


def get(facility_id: str) -> dict | None:
    return next((f for f in load() if f.get("id") == facility_id), None)


def is_linked(fac: dict) -> bool:
    return (fac or {}).get("link") == "ok"


# --- 모드 -------------------------------------------------------------------
def current_mode(db: Session | None = None) -> str:
    from . import settings
    mode = settings.get(KEY_MODE, db) or MODE_ADVISE
    return mode if mode in MODES else MODE_ADVISE


def set_mode(db: Session, mode: str) -> str | None:
    """제어 모드 변경. 이전 값을 돌려준다(감사 로그용)."""
    from . import settings
    if mode not in MODES:
        raise ValueError(f"알 수 없는 제어 모드: {mode}")
    return settings.set_value(db, KEY_MODE, mode)


# --- 명령 -------------------------------------------------------------------
def allowed_commands(mode: str) -> set[str]:
    """모드가 허용하는 명령.

    「권고만」에서 원격 제어가 나가면 모드 설정이 의미가 없다. 화면에서
    버튼을 숨기는 것과 별개로 여기서도 막는다 — 화면만 막으면 우회된다.
    """
    if mode == MODE_ADVISE:
        return {CMD_STATUS, CMD_ADVISE}
    return {CMD_STATUS, CMD_ADVISE, CMD_REMOTE}


def execute(db: Session, *, facility: dict, command: str, user,
            mode: str, event_id: int | None = None,
            memo: str = "") -> FacilityControl:
    """명령을 시도하고 결과를 기록한다.

    실제 시설 연동은 아직 없다. 규격을 확보하기 전까지는 「미연계」로 기록만
    남기며, 성공한 것처럼 보이지 않게 한다.
    """
    if command not in allowed_commands(mode):
        result = RESULT_BLOCKED
        detail = f"현재 모드({MODE_LABELS[mode]})에서 허용되지 않는 명령입니다."
    elif not is_linked(facility):
        result = RESULT_NOT_LINKED
        detail = ("시설 연동 규격이 확보되지 않아 실제 명령이 전달되지 않았습니다. "
                  "기록만 남습니다.")
    else:
        # 연동이 붙으면 이 자리에서 실제 프로토콜을 호출한다.
        result = RESULT_OK
        detail = "명령을 전달했습니다."

    row = FacilityControl(
        facility_id=facility.get("id", ""), facility_name=facility.get("name", ""),
        mode=mode, command=command, result=result,
        detail=(memo + " / " if memo else "") + detail,
        event_id=event_id, requested_by=getattr(user, "id", None),
        login_id=getattr(user, "login_id", "") or "")
    db.add(row)
    log.info("시설 명령 facility=%s cmd=%s mode=%s result=%s by=%s",
             facility.get("id"), command, mode, result,
             getattr(user, "login_id", "?"))
    return row


def history(db: Session, facility_id: str = "", limit: int = 100) -> list[FacilityControl]:
    stmt = select(FacilityControl)
    if facility_id:
        stmt = stmt.where(FacilityControl.facility_id == facility_id)
    return list(db.scalars(
        stmt.order_by(FacilityControl.created_at.desc(), FacilityControl.id.desc()).limit(limit)).all())
