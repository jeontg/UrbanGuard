"""CCTV 일괄 등록·내보내기 (S-80).

지점을 하나씩 화면에서 넣는 것은 7개소까지의 이야기다. 지자체 한 곳이
수십~수백 개소를 쓰면 손으로 못 넣는다. 세 경로를 같은 검증에 태운다.

  ① 교통정보 API 수집  → `cctv_sources.fetch()` → 여기로
  ② 엑셀 업로드        → `parse_excel()` → 여기로
  ③ 엑셀 내려받기      → `export_excel()`

**미리보기를 기본으로 둔다.** 잘못된 파일 한 장으로 DB가 더럽혀지면 되돌리기가
번거롭다. 확인한 뒤에만 실제로 넣는다.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import cameras as C
from .models import Camera
from .roles import DOMAIN_LABELS, DOMAIN_SHORT, Domain

log = logging.getLogger("urbanguard.camera_bulk")

DOMAINS = [d.value for d in Domain]

# 엑셀 열 정의 — (열 제목, 키, 설명). 내보내기와 업로드가 같은 표를 쓴다.
# 왕복이 되어야 「받아서 고쳐 올리는」 실무 흐름이 성립한다.
COLUMNS: list[tuple[str, str, str]] = [
    ("ID", "id", "대문자 시작, 영문 대문자·숫자·하이픈 3~64자 (예: CAM-SEOMYEON)"),
    ("이름", "name", "화면에 표시할 지점명"),
    ("담당부서", "dept", "부서담당자 권한 범위 판정에 쓰임"),
    ("시도", "sido", "영문 키 (seoul·busan·gyeongnam…). 비우면 좌표로 채움"),
    ("구군", "sigungu", "한글 이름 (강남구·기장군…). 시도를 비우면 넣을 수 없음"),
    ("위도", "lat", "33 ~ 39"),
    ("경도", "lng", "124 ~ 132"),
    ("소스종류", "source_type", "hls / video / synthetic"),
    ("스트림주소", "source_url", "소스종류가 hls 일 때"),
    ("파일경로", "source_path", "소스종류가 video 일 때"),
    ("원CCTV명", "cctv_name", "기관 CCTV 원 명칭(대조용)"),
    ("사용여부", "is_active", "Y / N"),
    ("비고", "note", ""),
    # ★ 2026-08-21: 도메인별 두 열은 손으로 나열하지 않고 DOMAINS 에서
    #   만든다. 예전에는 여기·기본값 dict·양식 예시 행 **세 곳**에 같은
    #   순서를 따로 적어 둬서, 도메인을 늘릴 때 한 곳만 빠뜨리면 예시 행의
    #   열 수가 어긋나 엉뚱한 칸에 값이 들어갔다(조용히 깨지는 종류다).
    *[(f"{DOMAIN_SHORT[d]}{suffix}", f"{prefix}_{d}", "Y / N")
      for d in DOMAINS
      for suffix, prefix in (("사용", "use"), ("상시", "cont"))],
]
HEADERS = [c[0] for c in COLUMNS]
KEY_BY_HEADER = {c[0]: c[1] for c in COLUMNS}

_TRUE = {"y", "yes", "예", "o", "true", "1", "사용", "상시"}


def _flag(v) -> bool:
    return str(v or "").strip().lower() in _TRUE


def _yn(v: bool) -> str:
    return "Y" if v else "N"


# --- ID 생성 ----------------------------------------------------------------
_ASCII = re.compile(r"[^A-Z0-9]+")


def suggest_id(name: str, seed: str = "", prefix: str = "CAM") -> str:
    """이름에서 ID 후보를 만든다.

    한글 이름은 그대로 ID가 될 수 없다(ID 규칙이 영문 대문자). 이름을 로마자로
    옮기는 것은 표기가 갈려 오히려 헷갈리므로, **짧은 해시**를 붙여 안정적이고
    중복 없는 값을 만든다. 같은 입력이면 항상 같은 ID가 나와, 다시 수집해도
    중복 등록되지 않는다.
    """
    base = unicodedata.normalize("NFKD", name or "").upper()
    base = _ASCII.sub("", base)[:24]
    h = hashlib.sha1(f"{name}|{seed}".encode()).hexdigest()[:6].upper()
    return f"{prefix}-{base}-{h}" if base else f"{prefix}-{h}"


def from_api_rows(rows: list[dict], *, prefix: str = "BSITS") -> list[dict]:
    """교통정보 API 결과를 등록 행 형태로. ID를 붙인다."""
    out = []
    for r in rows:
        out.append({
            "id": suggest_id(r["name"], r.get("source_url", ""), prefix),
            "name": r["name"],
            "dept": "",
            # 수집 API는 구·군을 주지 않는다. 시/도는 저장 단계에서 좌표로
            # 채워지고, 구·군은 **비워 둔다** — 지어내지 않는다.
            "sido": r.get("sido", ""),
            "sigungu": r.get("sigungu", ""),
            "lat": r["lat"], "lng": r["lng"],
            "source_type": r.get("source_type", "hls"),
            "source_url": r.get("source_url", ""),
            "source_path": "",
            "cctv_name": r.get("cctv_name", r["name"]),
            "is_active": True, "note": "",
            # 어느 탐지에 쓸지는 사람이 정한다. 전부 켜 두면 CPU가 감당 못 한다.
            **{f"{p}_{d}": False for d in DOMAINS for p in ("use", "cont")},
        })
    return out


# --- 검증 -------------------------------------------------------------------
def check(db: Session, rows: list[dict]) -> list[dict]:
    """행마다 상태를 매긴다 — 등록 전에 결과를 미리 보여 주기 위한 것.

    상태: ``new`` 신규 / ``dup`` 이미 있음 / ``error`` 오류
    """
    out: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=1):
        item = dict(row)
        item["_line"] = i
        cid = (row.get("id") or "").strip()
        errs = C.validate(row)
        # validate 는 db 를 넘기면 중복도 오류로 잡는다. 여기서는 중복을
        # 「오류」가 아니라 「갱신 대상」으로 따로 다루려고 db 를 넘기지 않았다.
        if cid and cid in seen:
            errs.append(f"파일 안에서 ID가 중복됩니다: {cid}")
        seen.add(cid)

        if errs:
            item["_status"] = "error"
            item["_errors"] = errs
        elif C.get(db, cid) is not None:
            item["_status"] = "dup"
            item["_errors"] = []
        else:
            item["_status"] = "new"
            item["_errors"] = []
        out.append(item)
    return out


def summarize(checked: list[dict]) -> dict:
    return {
        "total": len(checked),
        "new": sum(1 for r in checked if r["_status"] == "new"),
        "dup": sum(1 for r in checked if r["_status"] == "dup"),
        "error": sum(1 for r in checked if r["_status"] == "error"),
    }


# --- 등록 -------------------------------------------------------------------
def apply(db: Session, rows: list[dict], *, update_existing: bool = False) -> dict:
    """검증을 통과한 행만 등록한다. 오류 행은 건너뛴다.

    ``update_existing`` 이면 이미 있는 ID는 내용을 덮어쓴다. 기본은 건너뛰기다 —
    엑셀을 잘못 올려 운영 중인 지점 설정이 지워지는 것이 더 큰 사고다.
    """
    created = updated = skipped = failed = 0
    errors: list[str] = []
    for row in check(db, rows):
        status = row["_status"]
        if status == "error":
            failed += 1
            errors.append(f"{row['_line']}행 {row.get('id') or '(ID 없음)'}: "
                          + " / ".join(row["_errors"]))
            continue
        if status == "dup" and not update_existing:
            skipped += 1
            continue

        sel = {d: {"enabled": _flag(row.get(f"use_{d}")),
                   "continuous": _flag(row.get(f"cont_{d}"))} for d in DOMAINS}
        if status == "dup":
            cam, errs = C.update(db, row["id"].strip(), row)
            if errs:
                failed += 1
                errors.append(f"{row['_line']}행 {row['id']}: " + " / ".join(errs))
                continue
            updated += 1
        else:
            cam, errs = C.create(db, row)
            if errs:
                failed += 1
                errors.append(f"{row['_line']}행 {row['id']}: " + " / ".join(errs))
                continue
            created += 1
        if cam is not None:
            cam.is_active = _flag(row.get("is_active")) if row.get("is_active") is not None else True
            C.set_domains(db, cam, sel)

    log.info("일괄 등록 신규 %d · 갱신 %d · 건너뜀 %d · 실패 %d",
             created, updated, skipped, failed)
    return {"created": created, "updated": updated, "skipped": skipped,
            "failed": failed, "errors": errors}


# --- 엑셀 -------------------------------------------------------------------
def _require_openpyxl():
    try:
        import openpyxl  # noqa: F401
        return openpyxl
    except ImportError as e:
        raise RuntimeError(
            "엑셀 기능을 쓰려면 openpyxl 이 필요합니다. "
            "`pip install openpyxl` 후 서비스를 재시작하세요.") from e


def parse_excel(data: bytes) -> tuple[list[dict], list[str]]:
    """업로드된 엑셀을 행 목록으로. (행, 오류) 를 돌려준다."""
    openpyxl = _require_openpyxl()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as e:  # noqa: BLE001
        return [], [f"엑셀 파일을 열지 못했습니다: {str(e)[:80]}"]

    ws = wb.active
    it = ws.iter_rows(values_only=True)
    try:
        header = next(it)
    except StopIteration:
        return [], ["빈 파일입니다."]

    idx = {}
    for i, cell in enumerate(header or []):
        title = str(cell or "").strip()
        if title in KEY_BY_HEADER:
            idx[KEY_BY_HEADER[title]] = i
    missing = [h for h in ("ID", "이름", "위도", "경도", "소스종류")
               if KEY_BY_HEADER[h] not in idx]
    if missing:
        return [], [f"머리글에 필수 열이 없습니다: {', '.join(missing)}. "
                    "양식 내려받기로 받은 파일을 쓰세요."]

    rows: list[dict] = []
    for raw in it:
        if raw is None or all(c in (None, "") for c in raw):
            continue          # 빈 줄은 건너뛴다

        def cell(key, default=""):
            i = idx.get(key)
            if i is None or i >= len(raw):
                return default
            v = raw[i]
            return default if v is None else (v.strip() if isinstance(v, str) else v)

        rows.append({
            "id": str(cell("id")).strip().upper(),
            "name": str(cell("name")),
            "dept": str(cell("dept")),
            "lat": cell("lat"), "lng": cell("lng"),
            "source_type": str(cell("source_type")).strip().lower(),
            "source_url": str(cell("source_url")),
            "source_path": str(cell("source_path")),
            "cctv_name": str(cell("cctv_name")),
            "is_active": _flag(cell("is_active", "Y")),
            "note": str(cell("note")),
            **{f"use_{d}": _flag(cell(f"use_{d}")) for d in DOMAINS},
            **{f"cont_{d}": _flag(cell(f"cont_{d}")) for d in DOMAINS},
        })
    wb.close()
    return rows, []


def _sheet(wb, title: str):
    ws = wb.active
    ws.title = title
    return ws


def _style_header(ws, openpyxl) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    fill = PatternFill("solid", fgColor="1F3864")
    for i, name in enumerate(HEADERS, start=1):
        c = ws.cell(row=1, column=i, value=name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[c.column_letter].width = max(10, min(38, len(name) + 10))
    ws.freeze_panes = "A2"


def template_excel() -> bytes:
    """업로드 양식. 열 설명과 예시 한 줄을 넣어 준다."""
    openpyxl = _require_openpyxl()
    wb = openpyxl.Workbook()
    ws = _sheet(wb, "CCTV등록양식")
    _style_header(ws, openpyxl)

    # 도메인 열은 COLUMNS 와 **같은 순서로 생성**한다 — 손으로 적어 두면
    # 도메인이 늘 때 열 수가 어긋난다(위 COLUMNS 주석 참고).
    # 예시는 침수만 켠 지점으로 둔다.
    _example_domains = [("Y" if d == Domain.FLOOD.value else "N")
                        for d in DOMAINS for _ in range(2)]
    ws.append(["CAM-SEOMYEON", "서면교차로", "도로관리과", "busan", "부산진구",
               35.1579, 129.0594,
               "hls", "https://example.go.kr/live/cam1.m3u8", "", "서면_교차로_1",
               "Y", "예시 행 — 지우고 쓰세요",
               *_example_domains])

    guide = wb.create_sheet("작성안내")
    guide.append(["열", "설명"])
    for title, _key, desc in COLUMNS:
        guide.append([title, desc])
    guide.column_dimensions["A"].width = 14
    guide.column_dimensions["B"].width = 70
    guide.append([])
    guide.append(["주의", "ID는 등록 후 바꿀 수 없습니다 — 과거 이벤트가 이 값으로 묶입니다."])
    guide.append(["주의", "상시 탐지는 서비스를 재시작해야 반영됩니다."])
    guide.append(["주의", "상시로 켠 지점 수만큼 CPU를 씁니다. 한꺼번에 켜지 마세요."])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_excel(db: Session, camera_ids: set[str] | None = None) -> bytes:
    """등록된 CCTV·영상 전체를 엑셀로. **업로드 양식과 열이 같아 왕복된다.**

    받아서 고친 뒤 그대로 다시 올리는 것이 실무에서 가장 빠른 편집 방법이다.

    :param camera_ids: 지정하면 이 ID들만 내보낸다 — 부서담당자(MGR)가
        담당 도메인 밖 카메라까지 엑셀로 통째로 받아 가지 못하게 막는
        용도다(2026-08-22 전수점검, ``routes_cameras.cameras_export`` 참고).
    """
    openpyxl = _require_openpyxl()
    wb = openpyxl.Workbook()
    ws = _sheet(wb, "CCTV목록")
    _style_header(ws, openpyxl)

    cams = C.list_all(db)
    if camera_ids is not None:
        cams = [c for c in cams if c.id in camera_ids]
    roi = C.roi_status(db, cams)
    for cam in cams:
        dm = C.domain_map(cam)
        ws.append([
            cam.id, cam.name, cam.dept, cam.sido, cam.sigungu, cam.lat, cam.lng,
            cam.source_type, cam.source_url, cam.source_path, cam.cctv_name,
            _yn(cam.is_active), cam.note,
            *[v for d in DOMAINS
              for v in (_yn(dm[d]["enabled"]), _yn(dm[d]["continuous"]))],
        ])

    # 두 번째 시트에 현재 상태 요약 — 올릴 때는 안 읽고, 사람이 보는 용도다.
    info = wb.create_sheet("현황")
    info.append(["항목", "값"])
    info.append(["내보낸 시각", datetime.now(timezone.utc)
                 .astimezone().strftime("%Y-%m-%d %H:%M")])
    info.append(["등록 CCTV", len(cams)])
    for d in DOMAINS:
        rows = [c for c in cams if C.domain_map(c)[d]["enabled"]]
        cont = [c for c in rows if C.domain_map(c)[d]["continuous"]]
        info.append([f"{DOMAIN_LABELS[Domain(d)]} 사용",
                     f"{len(rows)}개소 (상시 {len(cont)} · 선택 {len(rows) - len(cont)})"])
    info.append([])
    info.append(["ROI 설정 현황", ""])
    for cam in cams:
        st = roi.get(cam.id) or {}
        # roi_status 는 도메인마다 dict 를 돌려준다 — dict 자체는 항상 참이라
        # ok 값을 봐야 「설정됨」을 제대로 가린다.
        have = [DOMAIN_LABELS[Domain(d)] for d in DOMAINS
                if (st.get(d) or {}).get("ok")]
        info.append([cam.name, ", ".join(have) if have else "없음"])
    info.column_dimensions["A"].width = 26
    info.column_dimensions["B"].width = 40

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
