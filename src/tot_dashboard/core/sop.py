"""디지털 SOP — 조치 단계 정의와 이행 기록 (S-86 편집 · S-03 이행).

**왜 만들었나.** 지금 이벤트 상세 화면은 「경계입니다」까지만 말한다. 그래서
무엇을 해야 하는지는 근무자의 기억과 벽에 붙은 코팅지에 달려 있다. 야간에 혼자
근무하는 상황실에서 그 둘은 믿을 것이 못 된다. 조치 순서를 화면에 옮겨 두면
화면이 대신 말해 주고, 무엇을 했는지가 자동으로 기록으로 남는다.

⚠️ **여기 심어 둔 기본 단계는 어느 기관의 행동매뉴얼도 아니다.**
지자체마다 통제 기준·통보 계통·전담 부서가 달라 우리가 정할 수 없다.
:data:`DEFAULT_STEPS` 는 「이 칸에 무엇을 적어야 하는가」를 보여 주는 뼈대이고,
내용은 **이 시스템이 이미 할 수 있는 행위**(부서 통보·기관 통보·시설물 제어
요청·조치 기록·종결)에서만 뽑았다. 도입 기관의 실제 매뉴얼로 교체해야 하며,
교체 전까지 화면은 이 단계들을 「기본안」으로 표시해 그 사실을 드러낸다.

**막지 않고 경고한다.** 필수 단계가 다 안 찍혀도 종결을 막지 않는다. 현장에는
규정대로 못 하는 상황이 실제로 있고, 막으면 사람은 아무 칸이나 찍고 넘어간다.
대신 「필수 n건 미이행」이 화면과 조치 이력에 남는다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import events as E
from .models import EventSopCheck, SopStep

# 도메인·등급 선택지에서 「전체」를 뜻하는 값.
ANY = ""
ANY_LABEL = "전체"

# 이벤트가 되는 등급만 대상이다 — 「관심」은 이벤트로 올리지 않는다
# (:data:`~.events.EVENT_THRESHOLD`).
SOP_LEVELS = tuple(lv for lv in E.LEVELS if E.is_reportable(lv))


# --- 기본 단계 (뼈대) --------------------------------------------------------
# 형식: (도메인, 등급, 제목, 설명, 필수여부)
#
# 도메인·등급이 ANY 면 모든 이벤트에 붙는다. 등급별 단계는 **그 등급 이상**에
# 붙지 않고 **그 등급에만** 붙는다 — 「경계」 이벤트에는 경계 단계와 공통 단계가
# 나오고 「주의」 단계는 나오지 않는다. 등급이 오르면 상위 단계가 새로 뜬다.
#
# 내용은 이 시스템이 실제로 지원하는 행위에서만 뽑았다. 「○○조에 따라」 같은
# 문구는 일부러 넣지 않았다 — 틀린 조문이 기본값으로 박히면 대장 전체가 틀린
# 근거로 채워진다(:mod:`.video_disclosure` 의 BASIS_PRESETS 와 같은 이유).
DEFAULT_STEPS: tuple[tuple[str, str, str, str, bool], ...] = (
    # 공통 — 도메인·등급 무관
    (ANY, ANY, "영상으로 현장 확인",
     "탐지 결과를 그대로 믿지 말고 해당 지점 영상을 눈으로 확인합니다. "
     "오탐이면 「오탐 신고」로 처리하고 여기서 끝냅니다.", True),
    (ANY, ANY, "상황 일지 기록",
     "확인한 내용과 판단 근거를 조치 기록에 남깁니다. "
     "교대 인수인계와 사후 검토가 이 기록에서 시작됩니다.", True),

    # 주의
    (ANY, "주의", "지속 관찰",
     "등급이 오르는지 지켜봅니다. 같은 지점의 다른 도메인 이벤트가 함께 열려 "
     "있는지도 확인합니다.", True),

    # 경계
    (ANY, "경계", "소관 부서 통보",
     "이벤트 상세의 「부서 통보」로 담당 부서에 알립니다. "
     "통보 대상 부서는 기관 조직도에 맞춰 알림 규칙(S-83)에서 정합니다.", True),

    # 심각
    (ANY, "심각", "유관기관 통보",
     "이벤트 상세의 「기관 통보」로 112·119·재난상황실에 알립니다. "
     "관제요원 단독으로 즉시 보낼 수 있습니다.", True),
    (ANY, "심각", "상황 종료 확인",
     "위험이 해소된 것을 영상으로 확인한 뒤 종결합니다. "
     "등급이 내려간 것만으로 종결하지 않습니다.", True),

    # 침수
    #
    # ★ 2026-08-21 flood/traffic 도메인 분리 검토 — 아래 두 단계는 행위가
    #   「차량 차단」이라 교통처럼 보이지만 **flood 에 남긴다.** SOP 는
    #   이벤트에 붙고, 이 두 단계를 발동시키는 것은 **침수 이벤트**(지하차도
    #   침수)이지 정체가 아니기 때문이다. 교통 정체 때문에 차단기를 내리지는
    #   않는다.
    ("flood", "경계", "차량 진입 차단 검토",
     "지하차도·저지대라면 차단 시설 상태를 확인하고 필요 시 차단을 권고합니다 "
     "(S-11 시설물 제어).", True),
    ("flood", "심각", "차단 시설 작동 확인",
     "차단기·경보등이 실제로 작동했는지 영상으로 확인합니다. "
     "요청만 하고 확인하지 않으면 통제된 줄 알고 넘어가게 됩니다.", True),

    # 교통 (2026-08-21 신설 도메인)
    #
    # ⚠️ **전용 단계를 두지 않았다.** 우회 안내·통제 요청 같은 교통 대응
    #   절차는 지자체 재난 대응 운영규정에 달려 있어 **우리가 지어낼 수
    #   없다.** 지금은 도메인 무관 단계(ANY — 통보·상황 종료 확인)가 그대로
    #   적용되며, 이는 빈틈이 아니라 「확정 전까지 기본 절차를 따른다」는
    #   뜻이다. 담당부서와 협의되면 여기에 추가한다.

    # 인파
    ("crowd", "경계", "밀집 구간과 이동 방향 파악",
     "어느 구간이 막혔고 사람이 어느 쪽으로 밀리는지 확인해 통보 내용에 적습니다. "
     "「인파 많음」만으로는 현장이 움직이지 못합니다.", True),
    ("crowd", "심각", "현장 인력 배치 요청",
     "부서·기관 통보 시 필요한 위치를 함께 전달합니다.", True),

    # 노면
    ("road", "경계", "보수 필요 여부 판단",
     "탐지된 파손이 즉시 보수 대상인지, 다음 정기 보수로 넘길 것인지 판단해 "
     "조치 기록에 남깁니다.", True),
    ("road", "심각", "긴급 보수 요청",
     "통행에 즉시 위험이 되는 파손이면 도로 부서에 긴급 보수를 요청합니다.", True),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- 기본값 심기 -------------------------------------------------------------

def seed_builtin(db: Session) -> int:
    """기본 단계를 심는다. 이미 있으면 건드리지 않는다.

    **덮어쓰지 않는 이유** — 기관이 고쳐 놓은 내용이 배포할 때마다 되돌아가면
    아무도 고치지 않는다. 지운 단계도 되살리지 않는다(지운 것은 의도다).
    """
    have = {(r.domain, r.level, r.title) for r in
            db.scalars(select(SopStep)).all()}
    added = 0
    # seq 는 (도메인, 등급) 안에서만 의미가 있다. 목록에 적은 순서를 그대로 쓴다.
    counters: dict[tuple[str, str], int] = {}
    for domain, level, title, detail, required in DEFAULT_STEPS:
        key = (domain, level)
        counters[key] = counters.get(key, 0) + 10
        if (domain, level, title) in have:
            continue
        db.add(SopStep(domain=domain, level=level, seq=counters[key],
                       title=title, detail=detail, required=required,
                       builtin=True, is_active=True, updated_by="system"))
        added += 1
    if added:
        db.flush()
    return added


def builtin_ratio(db: Session) -> tuple[int, int]:
    """(기본안 그대로인 단계 수, 전체 단계 수).

    화면에 그대로 띄운다 — 「기관 매뉴얼로 바꿨는가」를 숫자로 보여 주지 않으면
    아무도 바꾸지 않은 채 납품된다.
    """
    total = db.execute(select(func.count(SopStep.id))
                       .where(SopStep.is_active.is_(True))).scalar() or 0
    stock = db.execute(select(func.count(SopStep.id))
                       .where(SopStep.is_active.is_(True),
                              SopStep.builtin.is_(True))).scalar() or 0
    return stock, total


# --- 단계 정의 ---------------------------------------------------------------

def all_steps(db: Session, *, domain: str = "", level: str = "",
              include_inactive: bool = True) -> list[SopStep]:
    """편집 화면용 목록. 필터는 **정확히 일치**로 좁힌다(전체 단계 조회용)."""
    stmt = select(SopStep)
    if domain:
        stmt = stmt.where(SopStep.domain == domain)
    if level:
        stmt = stmt.where(SopStep.level == level)
    if not include_inactive:
        stmt = stmt.where(SopStep.is_active.is_(True))
    return list(db.scalars(stmt.order_by(SopStep.domain, SopStep.level,
                                         SopStep.seq, SopStep.id)).all())


def steps_for(db: Session, domain: str, level: str) -> list[SopStep]:
    """이 이벤트에 붙는 단계. 공통(ANY) 단계가 함께 나온다.

    등급은 **정확히 일치**한다 — 「경계」 이벤트에 「주의」 단계까지 붙이면
    체크리스트가 길어져 아무도 안 읽는다. 등급이 오르면 상위 단계가 새로 뜨고,
    이미 찍은 하위 등급 기록은 그대로 남는다.
    """
    stmt = (select(SopStep)
            .where(SopStep.is_active.is_(True),
                   or_(SopStep.domain == domain, SopStep.domain == ANY),
                   or_(SopStep.level == level, SopStep.level == ANY))
            # 공통 단계를 먼저, 그다음 도메인 단계. 등급 무관을 먼저.
            .order_by(SopStep.level, SopStep.domain, SopStep.seq, SopStep.id))
    return list(db.scalars(stmt).all())



# --- 위험유형별 SOP (S-98 · hazard_sop_map) ---------------------------------
#
# ★ 왜 필요한가
#     지금 SOP 는 **「도메인 × 등급」** 두 축으로만 고른다. 그래서 「침수」면
#     지하차도든 배수로든 **같은 체크리스트**가 나온다. 실제로는 조치가 다르다 —
#     지하차도는 **차량 진입 차단이 최우선**이고 배수로는 배수 조치다.
#
#     경남 제안요청서 **SFR-012** 는 「단계별 SOP 를 자동으로 **도, 시·군별로**
#     제시」를 요구한다. 그 「도, 시·군별」이 ``hazard_sop_map.zone_id`` 다.
#
# ★ 어떻게 합치는가 — **더한다(합집합)**, 갈아치우지 않는다
#     세분류는 대분류의 조치를 **포함하고 더 있다.** 지하차도 침수도 침수라서
#     「영상으로 현장 확인」·「차량 진입 차단 검토」는 그대로 필요하다.
#
#     ⚠️ 갈아치우게 만들면, 매핑을 한 줄만 넣어 둔 순간 **공통 단계가 통째로
#     사라진다.** 「영상으로 현장 확인」이 빠진 체크리스트가 되는 것이다.
#     그건 지금보다 나쁘다.
#
# ⚠️ 매핑 전용 단계의 ``domain`` 에는 **위험유형 코드**가 들어간다
#     ``sop_steps.domain`` 은 원래 도메인(flood/crowd/road)이다. 그런데 세분류
#     전용 단계에 ``domain='flood'`` 을 주면 **fallback 매칭에 걸려 「침수」
#     전체에 지하차도 단계가 붙는다.** 도메인 목록에 없는 값이면 fallback 이
#     자연히 비껴가므로 그 성질을 쓴다 — 이 단계들은 **오직 매핑을 통해서만**
#     나온다.

# 형식: (위험유형코드, 등급, 제목, 설명, 필수여부)
#
# ⚠️ 여기 있는 것도 **어느 기관의 행동매뉴얼이 아니다.** DEFAULT_STEPS 와 같은
# 이유로, 이 시스템이 실제로 할 수 있는 행위에서만 뽑은 뼈대다.
DEFAULT_HAZARD_STEPS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("flood_underpass", "경계", "지하차도 진입 차단 우선 검토",
     "지하차도는 물이 고이면 빠져나갈 곳이 없습니다. 저지대보다 **먼저** "
     "차단을 검토하고, 차단 요청 시각을 조치 기록에 남깁니다.", True),
    ("flood_underpass", "심각", "차도 내 차량 잔류 확인",
     "차단 전에 들어간 차량이 안에 남아 있는지 영상으로 확인합니다. "
     "차단만 하고 안을 보지 않으면 갇힌 차량을 놓칩니다.", True),

    ("flood_drainage", "경계", "배수 상태와 역류 여부 확인",
     "배수로가 막혔는지, 물이 역류하는지 확인해 조치 기록에 구분해 적습니다. "
     "막힘과 역류는 부르는 부서가 다릅니다.", True),

    ("crowd_loitering", "경계", "동일 인물 반복 여부 확인",
     "같은 사람이 계속 머무는 것인지, 서로 다른 사람이 오가는 것인지 "
     "영상으로 구분합니다. 판단이 달라집니다.", True),

    ("road_pothole", "경계", "차로 위치와 크기 기록",
     "몇 차로인지, 통행에 지장을 주는 크기인지 조치 기록에 남깁니다. "
     "보수 부서가 현장을 찾는 데 필요한 정보입니다.", True),
)

# 형식: (위험유형코드, 등급, 구역, 단계제목)
#
# ``level_code``·``zone_id`` 가 빈 문자열이면 「전체」다.
# 구역은 기관 지형에 따라 달라 **기본값을 넣지 않는다** — 지어내면 틀린 값이
# 기본으로 박힌다. 화면(S-98)에서 기관이 정합니다.
DEFAULT_HAZARD_MAP: tuple[tuple[str, str, str, str], ...] = tuple(
    (code, level, "", title)
    for code, level, title, _detail, _req in DEFAULT_HAZARD_STEPS)


def seed_hazard_sop(db: Session) -> tuple[int, int]:
    """위험유형별 단계와 매핑을 심는다. ``(단계 수, 매핑 수)``.

    :func:`seed_builtin` 과 같은 규칙이다 — **이미 있으면 건드리지 않고,
    지운 것은 되살리지 않는다.**
    """
    from .models import HazardSopMap, HazardType

    known = {r.code for r in db.scalars(select(HazardType)).all()}
    have_step = {(r.domain, r.level, r.title): r
                 for r in db.scalars(select(SopStep)).all()}
    added_step = 0
    counters: dict[tuple[str, str], int] = {}
    for code, level, title, detail, required in DEFAULT_HAZARD_STEPS:
        # ⚠️ 없는 위험유형에 매달면 FK 가 걸린다. 어휘가 먼저다.
        if code not in known:
            continue
        key = (code, level)
        counters[key] = counters.get(key, 0) + 10
        if (code, level, title) in have_step:
            continue
        row = SopStep(domain=code, level=level, seq=counters[key], title=title,
                      detail=detail, required=required, builtin=True,
                      is_active=True, updated_by="system")
        db.add(row)
        have_step[(code, level, title)] = row
        added_step += 1
    if added_step:
        db.flush()          # id 가 있어야 매핑이 걸린다

    have_map = {(r.hazard_type_code, r.level_code, r.zone_id, r.sop_step_id)
                for r in db.scalars(select(HazardSopMap)).all()}
    added_map = 0
    seq = 0
    for code, level, zone, title in DEFAULT_HAZARD_MAP:
        if code not in known:
            continue
        step = have_step.get((code, level, title))
        if step is None or step.id is None:
            continue
        seq += 10
        if (code, level, zone, step.id) in have_map:
            continue
        db.add(HazardSopMap(hazard_type_code=code, level_code=level,
                            zone_id=zone, sop_step_id=step.id, seq=seq))
        added_map += 1
    if added_map:
        db.flush()
    return added_step, added_map


def mapped_steps(db: Session, hazard_type_code: str, level: str,
                 zone_ids: list[str] | None = None) -> list[SopStep]:
    """매핑표가 지정한 단계. 매핑이 없으면 빈 목록.

    ``zone_id`` 가 빈 문자열인 행은 **모든 구역에** 적용된다. 지점이 속한
    구역이 있으면 그 구역 전용 행이 함께 나온다.
    """
    from .models import HazardSopMap

    code = (hazard_type_code or "").strip()
    if not code:
        return []
    zones = [z for z in (zone_ids or []) if z]
    stmt = (select(SopStep)
            .join(HazardSopMap, HazardSopMap.sop_step_id == SopStep.id)
            .where(SopStep.is_active.is_(True),
                   HazardSopMap.hazard_type_code == code,
                   or_(HazardSopMap.level_code == level,
                       HazardSopMap.level_code == ANY),
                   or_(HazardSopMap.zone_id == ANY,
                       HazardSopMap.zone_id.in_(zones) if zones
                       else HazardSopMap.zone_id == ANY))
            .order_by(HazardSopMap.seq, SopStep.seq, SopStep.id))
    return list(db.scalars(stmt).all())

def save_step(db: Session, *, step_id: int | None, domain: str, level: str,
              title: str, detail: str = "", seq: int = 0,
              required: bool = True, is_active: bool = True,
              by: str = "") -> SopStep:
    """단계 등록·수정. 고치면 ``builtin`` 표시가 떨어진다 —
    기관이 손댄 순간 더 이상 「기본안」이 아니다."""
    title = (title or "").strip()[:160]
    if not title:
        raise ValueError("단계 제목을 입력하세요.")
    if domain and domain not in {d.value for d in _domains()}:
        raise ValueError(f"도메인이 올바르지 않습니다: {domain}")
    if level and level not in SOP_LEVELS:
        raise ValueError(f"등급이 올바르지 않습니다: {level}")

    row = db.get(SopStep, step_id) if step_id else None
    if row is None:
        row = SopStep(domain=domain, level=level)
        db.add(row)
    row.domain, row.level = domain, level
    row.title, row.detail = title, (detail or "").strip()[:4000]
    row.seq, row.required, row.is_active = int(seq), bool(required), bool(is_active)
    row.builtin = False
    row.updated_by = (by or "")[:64]
    row.updated_at = _now()
    db.flush()
    return row


def delete_step(db: Session, step_id: int) -> bool:
    """단계를 지운다. **이행 기록은 남는다** — ``step_id`` 만 비고 제목은
    ``step_title`` 에 적혀 있다. 「무엇을 했는지」가 사라지면 안 된다."""
    row = db.get(SopStep, step_id)
    if row is None:
        return False
    db.execute(sa_delete(SopStep).where(SopStep.id == step_id))
    db.flush()
    return True


def _domains():
    from .roles import Domain
    return Domain


# --- 이행 기록 ---------------------------------------------------------------

def checks_for(db: Session, event_id: int) -> dict[int, EventSopCheck]:
    """단계 ID → 이행 기록. 단계가 지워진 기록은 빠진다(키가 ``None``)."""
    rows = db.scalars(select(EventSopCheck)
                      .where(EventSopCheck.event_id == event_id)).all()
    return {r.step_id: r for r in rows if r.step_id is not None}


def history(db: Session, event_id: int) -> list[EventSopCheck]:
    """이행 기록 전체(지워진 단계 포함), 찍은 순서대로."""
    return list(db.scalars(
        select(EventSopCheck).where(EventSopCheck.event_id == event_id)
        .order_by(EventSopCheck.checked_at, EventSopCheck.id)).all())


def check(db: Session, event_id: int, step_id: int, *, user=None,
          skipped: bool = False, note: str = "") -> EventSopCheck:
    """단계를 이행(또는 「해당 없음」)으로 표시한다.

    「해당 없음」은 사유가 있어야 한다 — 사유 없이 넘길 수 있으면 체크리스트가
    한 번에 다 넘어가고, 그러면 아무 의미가 없다.
    """
    note = (note or "").strip()[:2000]
    if skipped and not note:
        raise ValueError("「해당 없음」으로 넘기려면 사유를 입력하세요.")
    step = db.get(SopStep, step_id)
    if step is None:
        raise ValueError("대상 단계를 찾을 수 없습니다.")

    row = db.scalars(
        select(EventSopCheck).where(EventSopCheck.event_id == event_id,
                                    EventSopCheck.step_id == step_id)).first()
    if row is None:
        row = EventSopCheck(event_id=event_id, step_id=step_id)
        db.add(row)
    row.step_title = step.title
    row.skipped, row.note = bool(skipped), note
    row.checked_at = _now()
    if user is not None:
        row.user_id, row.login_id = user.id, user.login_id
    db.flush()
    return row


def uncheck(db: Session, event_id: int, step_id: int) -> bool:
    """잘못 찍은 것을 해제한다. 해제 사실은 부르는 쪽이 조치 이력에 남긴다
    (:func:`~.events.add_action`) — 여기서는 행만 지운다."""
    n = db.execute(sa_delete(EventSopCheck)
                   .where(EventSopCheck.event_id == event_id,
                          EventSopCheck.step_id == step_id)).rowcount
    db.flush()
    return bool(n)



def zones_of_event(db: Session, event) -> list[str]:
    """이 이벤트가 난 지점이 속한 구역 id 목록. 못 찾으면 빈 목록.

    ⚠️ **실패해도 예외를 올리지 않는다.** 구역을 못 읽었다고 SOP 가 통째로
    안 뜨면, 관제요원은 「할 일이 없다」로 읽는다. 구역 전용 단계만 빠지고
    나머지는 그대로 나오는 편이 낫다.
    """
    from .models import CameraZone
    cam = (getattr(event, "block_id", "") or "").strip()
    if not cam:
        return []
    try:
        return [r.zone_id for r in db.scalars(
            select(CameraZone).where(CameraZone.camera_id == cam)).all()]
    except Exception:  # noqa: BLE001
        return []


def steps_for_event(db: Session, event) -> list[SopStep]:
    """이 이벤트에 붙는 단계 — **기본 매칭 + 위험유형 매핑(합집합)**.

    ★ **더한다. 갈아치우지 않는다.** 세분류는 대분류의 조치를 포함하고 더
    있다 — 지하차도 침수도 침수라서 「영상으로 현장 확인」은 그대로 필요하다.
    매핑을 한 줄 넣었다고 공통 단계가 사라지면 그건 지금보다 나쁘다.

    ⚠️ **매핑이 비어 있으면 지금과 완전히 같다.** 그래서 회귀 위험이 없다.
    """
    base = steps_for(db, event.domain, event.level)
    extra = mapped_steps(db, getattr(event, "hazard_type_code", "") or "",
                         event.level, zones_of_event(db, event))
    if not extra:
        return base
    seen = {s.id for s in base}
    return base + [s for s in extra if s.id not in seen]


def progress(db: Session, event) -> dict:
    """이 이벤트의 이행 현황.

    ``required_left`` 가 0이 아니면 화면이 종결 버튼 옆에 경고를 띄운다.
    막지는 않는다 — 이 모듈 머리말 참고.
    """
    steps = steps_for_event(db, event)
    done = checks_for(db, event.id)
    left = [s for s in steps if s.required and s.id not in done]
    return {"steps": steps, "checks": done,
            "total": len(steps), "done": len(
                [s for s in steps if s.id in done]),
            "required_left": len(left), "left_titles": [s.title for s in left]}


def summary_text(db: Session, event) -> str:
    """조치 이력·인수인계에 넣을 한 줄. 「3/5 (필수 1건 미이행)」."""
    p = progress(db, event)
    if not p["total"]:
        return "SOP 단계 없음"
    s = f"SOP {p['done']}/{p['total']}"
    if p["required_left"]:
        s += f" (필수 {p['required_left']}건 미이행)"
    return s
