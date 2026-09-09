"""테이블 정의 — docs/ui_design_spec.md 4절 「DB 스키마로의 귀결」.

네 테이블 모두 권한 매트릭스에서 직접 도출됐다. 특히 ``notifications`` 의
requested_by / approved_by 두 컬럼은 「2인 승인」 정책이 스키마로 나타난 것이다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .roles import Role


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """운영 계정. 비밀번호는 해시만 저장하며 평문은 어디에도 남기지 않는다."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    dept: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(8), nullable=False, default=Role.OPR.value)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    pw_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    pw_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # 임시 비밀번호로 발급·초기화된 계정은 본인이 바꾸기 전까지 다른 화면을
    # 쓸 수 없다. 발급자가 아는 비밀번호가 계속 살아 있으면 안 되기 때문이다.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                       default=False)
    # 로그인 실패 누적과 잠금 해제 시각. 설계서 5절 「5회 실패 시 계정 잠금」.
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now,
                                                 onupdate=_now)

    domains: Mapped[list["UserDomain"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin")

    @property
    def domain_set(self) -> set[str]:
        return {d.domain for d in self.domains}


class UserDomain(Base):
    """부서담당자(MGR)의 담당 도메인 매핑.

    설계서 4절 주석 5 — MGR은 배정된 도메인에 한해 권한을 가진다.
    SYS는 이 매핑과 무관하게 전 도메인을 다룬다.
    """

    __tablename__ = "user_domains"
    __table_args__ = (UniqueConstraint("user_id", "domain", name="uq_user_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"),
                                         nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)

    user: Mapped[User] = relationship(back_populates="domains")


class SignupRequest(Base):
    """가입 신청 (2026-09-01 신설) — 로그인 화면의 셀프서비스 가입 신청을
    관리자가 검토해 실제 계정으로 등록하는 흐름의 상태.

    ``core/notifications.py::Notification``(S-50 알림 승인)과 같은 모양의
    "요청 → 승인/거절" 상태표다 — 그 파일이 이미 검증된 패턴이라 그대로
    따른다.

    ``requested_domains``를 :class:`UserDomain` 처럼 별도 조인 테이블로
    만들지 않은 이유 — 승인 전까지는 참고 정보일 뿐 실제 권한 판정에
    쓰이지 않는다(권한 판정은 승인 후 만들어진 진짜 ``UserDomain`` 행이
    한다). 그래서 콤마로 구분한 문자열 컬럼 하나로 충분하다.

    ``pw_hash``를 신청 시점에 미리 저장하는 이유 — 신청자가 직접 정한
    비밀번호를 관리자가 알 필요도, 다시 물을 필요도 없게 한다. 관리자는
    역할·담당 도메인만 결정해 승인하고, 그 순간 이 해시를 그대로
    :class:`User` 로 옮긴다(:func:`~.bootstrap.create_user` 의
    ``pw_hash=`` 인자 참고). 평문 비밀번호는 이 표를 포함해 어디에도
    남지 않는다.
    """

    __tablename__ = "signup_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    dept: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    requested_role: Mapped[str] = mapped_column(String(8), nullable=False,
                                                default=Role.OPR.value)
    requested_domains: Mapped[str] = mapped_column(String(255), nullable=False,
                                                   default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pw_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # pending -> approved / rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default="pending")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 승인으로 실제 만들어진 계정. 신청 기록에서 "그래서 어떤 계정이
    # 됐나"를 되짚을 수 있게 한다 — 계정이 나중에 지워져도 신청 기록
    # 자체는 남아야 하므로 SET NULL.
    created_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_signup_requests_status", "status"),
        Index("ix_signup_requests_login_status", "login_id", "status"),
        Index("ix_signup_requests_ip_created", "ip", "created_at"),
    )


class AuditLog(Base):
    """감사 추적. 공공기관 감사 대응의 근거가 되므로 삭제·수정하지 않는다."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 사용자가 지워져도 로그는 남아야 하므로 ondelete=SET NULL.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    login_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    dept: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)


Index("ix_audit_created", AuditLog.created_at.desc())
Index("ix_audit_dept", AuditLog.dept)


class VideoDisclosure(Base):
    """영상 반출 관리대장 (S-94) — 누구에게 어떤 영상을 언제 왜 내줬는가.

    「지방자치단체 영상정보처리기기 통합관제센터 구축 및 운영 규정」과
    개인정보 보호법이 요구하는 기록이다. **감사 로그와 같은 원칙으로
    지우지 않는다** — 잘못 적었으면 정정 사유를 남기고 새 줄을 만든다.

    ⚠️ **요청자 성명·연락처가 개인정보다.** 열람 권한을 좁게 두고
    (`roles.VIDEO_DISCLOSURE`), 보존기간이 지나면 파기해야 한다 —
    파기 시점은 기관 규정을 따르며 `disposal_due` 에 적어 둔다.
    """

    __tablename__ = "video_disclosures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- 요청 ---
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   nullable=False)
    requester_org: Mapped[str] = mapped_column(String(128), nullable=False)
    requester_name: Mapped[str] = mapped_column(String(64), nullable=False,
                                                default="")
    requester_contact: Mapped[str] = mapped_column(String(64), nullable=False,
                                                   default="")
    # 법적 근거. 「형사소송법 제○조」·「영장」·「정보주체 본인 열람」 등.
    # **이것 없이 내주면 안 된다.** 그래서 필수다.
    legal_basis: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- 대상 ---
    camera_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    period_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- 처리 ---
    # view(열람) / copy(사본 제공) / original(원본 제공)
    method: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default="view")
    # 제3자 비식별 처리 여부. 본인 열람 시 제3자는 모자이크가 원칙이다.
    masked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handler_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    handler_login: Mapped[str] = mapped_column(String(64), nullable=False,
                                               default="")

    # --- 파기 ---
    disposal_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 정정 이력. 지우는 대신 새 줄을 만들고 원본을 가리킨다.
    corrects_id: Mapped[int | None] = mapped_column(
        ForeignKey("video_disclosures.id", ondelete="SET NULL"))
    correction_reason: Mapped[str] = mapped_column(Text, nullable=False,
                                                   default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False,
                                            default="")


Index("ix_disclosure_requested", VideoDisclosure.requested_at.desc())
Index("ix_disclosure_org", VideoDisclosure.requester_org)


class Notification(Base):
    """주민·부서 알림 발송 건. 2인 승인 흐름의 상태를 그대로 담는다.

    status: requested -> approved -> sent / rejected / failed
    OPR이 「심각」 단계에서 단독 발송한 경우 status 는 sent 이면서
    approved_by 가 비어 있고 needs_post_approval 이 True 로 남는다.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="sms")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recipients_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="requested")
    needs_post_approval: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                      default=False)
    reject_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   default=_now, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_notification_status", Notification.status)
Index("ix_notification_requested", Notification.requested_at.desc())


class Event(Base):
    """이벤트 — 관제요원이 다루는 1급 단위 (S-02 / S-03).

    도메인(침수·인파·노면)은 이벤트의 **속성**일 뿐이다. 관제요원은 도메인별로
    근무하지 않고 「지금 처리해야 할 사건」 목록을 본다
    (docs/ui_design_spec.md 3절 「이벤트 중심」).

    한 지점에서 위험이 계속되는 동안 이벤트가 계속 새로 생기면 큐가 쓸모없어
    지므로, **기본적으로 같은 (도메인, 지점)에 열려 있는 이벤트는 하나만**
    두고 등급과 관측값만 갱신한다.

    ★ 2026-08-26 — ``core.events.record_detection(split_by_hazard_type=True)``
    를 쓰면 위험유형(``hazard_type_code``)까지 같아야 같은 이벤트로 본다.
    같은 카메라에서 **동시에 일어날 수 있는 독립 사건**(보행자 도로 진입·
    역주행 의심·사고 의심 등, 교통위험 돌발상황 확장)이 서로를 덮어쓰지
    않게 하려는 것이다(`docs/202608260842/` 참고). 기본값은 False로
    남긴다 — 강우 정체→정지차량 다발→교통마비처럼 **한 상황이 악화되는
    과정**은 여전히 하나로 합쳐야 하고, 침수·인파·노면도 유형을 나누지
    않는다.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    block_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    place_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # 최고 등급을 따로 남긴다 — 등급이 내려가도 「경계까지 갔던 건」임을
    # 사후에 알아야 대응 적정성을 판단할 수 있다.
    peak_level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    confidence: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[dict | None] = mapped_column(JSONB)

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)
    # ⚠️ 2026-09-02 신설(이벤트 자동 보류 정책) — `updated_at`은 20초 주기
    # 동기화 루프가 등급과 무관하게 매번 갱신하고, 사람이 확인/종결만
    # 눌러도 SQLAlchemy onupdate로 같이 갱신돼 "위험이 임계등급 이상으로
    # 다시 관측됐는가"의 근거로 쓸 수 없다(직접 실측 확인). 이 칸은
    # `core/events.py::record_detection()`이 `is_reportable(level)`일
    # 때만 갱신한다 — "재탐지 없음"을 정확히 판정하기 위한 전용 컬럼.
    last_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    closed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    false_positive: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                 default=False)

    # 위험유형 어휘와 잇는 칸(:class:`HazardType`). ⚠️ 기존 ``domain``·
    # ``event_type`` 문자열과 **병행**한다 — 새 이벤트부터 채우고 과거는 NULL
    # 로 남긴다. 한 번에 갈아치우면 지난 이력이 끊긴다.
    hazard_type_code: Mapped[str | None] = mapped_column(String(32))

    actions: Mapped[list["EventAction"]] = relationship(
        back_populates="event", cascade="all, delete-orphan",
        order_by="EventAction.created_at", lazy="selectin")


Index("ix_event_status", Event.status)
Index("ix_event_detected", Event.detected_at.desc())
Index("ix_event_domain_block", Event.domain, Event.block_id)
# 열린 이벤트 조회가 이제 유형까지 함께 본다(open_event_for) — 기존 인덱스는
# 지우지 않는다. 다른 조회(목록·필터)가 여전히 (도메인, 지점) 만으로도 쓴다.
Index("ix_event_domain_block_hazard", Event.domain, Event.block_id,
      Event.hazard_type_code)


class EventAction(Base):
    """이벤트에 가한 조치 이력. 감사 대응의 근거이므로 수정·삭제하지 않는다."""

    __tablename__ = "event_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    login_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)

    event: Mapped[Event] = relationship(back_populates="actions")


class Camera(Base):
    """CCTV 감시 지점 (S-80).

    **세 도메인이 같은 카메라를 공유한다.** 초량 CCTV 하나로 침수도 보고 인파도
    보는 것이 실제 운영 방식이므로, 카메라 자체와 도메인별 설정을 분리한다.

    기본키를 문자열 ID로 둔 이유 — 기존 ``blocks.json`` 의 ``BLOCK-CHORYANG`` 같은
    ID가 ``events.block_id`` 와 ROI 파일명에 이미 쓰이고 있다. 정수 PK로 바꾸면
    그 연결이 전부 끊긴다.
    """

    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    dept: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)

    # 행정구역. **좌표에서 유추하지 않고 입력받아 저장한다** — 사각형으로는
    # 구·군을 가를 수 없다. 비어 있으면 화면에서 「지역 미상」으로 보인다.
    # sido 는 `cctv_sources.REGIONS` 의 키(seoul·busan…), sigungu 는 한글 이름.
    sido: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    sigungu: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    source_type: Mapped[str] = mapped_column(String(16), nullable=False,
                                             default="hls")
    source_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    cctv_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- 방향 정보 (S-63 사각지대 분석의 선행 조건) ---------------------------
    #
    # 근거는 KLID 제안요청서 SFR-14 「CCTV 의 좌표정보 및 **방향각(상하/좌우)**
    # 을 활용해 GIS 시각화」「각 CCTV 를 **설치 목적별로 분류**」다.
    #
    # ⚠️ **값은 비워 둔 채 시작한다.** 등록된 지점의 실제 방향각을 우리는
    # 모르고, 좌표만으로는 알 수 없다. 추측해서 채우면 커버리지 분석이 통째로
    # 거짓이 된다. S-80 에서 사람이 입력한다.
    bearing_deg: Mapped[int | None] = mapped_column(Integer)   # 방위 0~359
    tilt_deg: Mapped[int | None] = mapped_column(Integer)      # 상하 각도
    fov_deg: Mapped[int | None] = mapped_column(Integer)       # 화각
    # crime(방범) / disaster(재난) / traffic(교통) / facility(시설) …
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)

    domains: Mapped[list["CameraDomain"]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", lazy="selectin")
    rois: Mapped[list["CameraRoi"]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", lazy="selectin")

    def domain_row(self, domain: str) -> "CameraDomain | None":
        return next((d for d in self.domains if d.domain == domain), None)

    def roi_row(self, domain: str) -> "CameraRoi | None":
        return next((r for r in self.rois if r.domain == domain), None)


class CameraDomain(Base):
    """카메라 × 도메인 사용 설정.

    ``continuous`` 가 이 테이블의 핵심이다.

    - **True(상시 분석)** — 파이프라인이 계속 돌린다. 위험이 언제 올지 모르는
      침수 지점이 여기 해당한다
    - **False(선택 분석)** — 관제요원이 화면에서 카메라를 고를 때만 분석한다.
      인파·노면은 검출 부하가 커서 전 지점 상시 분석이 현실적이지 않다

    카메라가 수십 개로 늘면 침수도 주요 지점만 상시로 돌려야 하므로,
    도메인 고정이 아니라 **카메라마다 관리자가 정하도록** 했다.
    """

    __tablename__ = "camera_domains"
    __table_args__ = (UniqueConstraint("camera_id", "domain",
                                       name="uq_camera_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    continuous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 도메인별 부가 설정 — 침수의 rainfall/river/rain, 인파의 loiter_sec 등.
    # 도메인마다 항목이 달라 컬럼으로 고정하면 도메인 추가 때마다 마이그레이션이
    # 필요해진다.
    config: Mapped[dict | None] = mapped_column(JSONB)

    camera: Mapped[Camera] = relationship(back_populates="domains")


class CameraRoi(Base):
    """카메라 × 도메인별 관심영역.

    **같은 카메라라도 도메인마다 영역이 다르다.** 침수는 「도로 면적」, 인파는
    「침입 금지 구역」, 노면은 「분석 구간」을 본다. 그래서 (카메라, 도메인)
    단위로 저장한다.

    ``shapes`` 를 JSONB 로 둔 이유도 같다 — 도메인마다 필요한 도형 종류가 다르다.
      flood: road_roi / low_point_roi / lane_threshold_line
      crowd: intrusion_roi
      road:  analysis_roi
    """

    __tablename__ = "camera_rois"
    __table_args__ = (UniqueConstraint("camera_id", "domain",
                                       name="uq_camera_roi"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    frame_width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frame_height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shapes: Mapped[dict | None] = mapped_column(JSONB)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)

    camera: Mapped[Camera] = relationship(back_populates="rois")


class CitizenReport(Base):
    """현장 제보 (S-43).

    노면 학습 데이터를 해외 공개데이터로 확보하려던 시도가 세 번 실패했다
    (도메인 갭·라벨 부재). **운영하면서 부산 데이터를 직접 쌓는 것**이 현재로선
    가장 현실적인 경로다(설계서 5절).

    ⚠️ 사진에는 사람·차량번호가 함께 찍힌다. 원본은 보관하지 않고 받는 즉시
    가려서 저장하며, 그 상태를 ``mask_status`` 에 남긴다.
    """

    __tablename__ = "citizen_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(16), nullable=False, default="road")
    place_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    block_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    photo_path: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    mask_status: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    mask_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # received -> reviewed -> converted / rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    disposition: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 재학습 데이터로 쓸 수 있는지 담당자가 판정한다.
    usable_for_training: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                      default=False)

    reported_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    login_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_report_status", CitizenReport.status)
Index("ix_report_created", CitizenReport.created_at.desc())


class FacilityControl(Base):
    """시설물 제어 시도 기록 (S-11).

    **성공·실패를 가리지 않고 모두 남긴다.** 차단막이 내려갔는지 여부는
    인명과 직결되므로, 「시도했으나 실패」가 기록되지 않으면 사후에 책임
    소재를 가릴 수 없다(설계서 4절).
    """

    __tablename__ = "facility_controls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    facility_id: Mapped[str] = mapped_column(String(64), nullable=False)
    facility_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 시도 당시의 제어 모드. 나중에 모드가 바뀌어도 그때 상태를 알아야 한다.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="advise")
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"))
    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    login_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)


Index("ix_facility_created", FacilityControl.created_at.desc())


class AppSetting(Base):
    """운영자가 화면에서 바꾸는 설정값 (S-85).

    키-값 한 쌍짜리 단순 구조다. 항목이 늘 때마다 컬럼을 추가하고 마이그레이션을
    돌리는 대신, 화면에서 다루는 소수의 값만 여기에 담는다. 스키마가 중요한
    설정(블록·ROI·임계값)은 각자의 테이블/파일을 쓴다.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)


class RoadInspection(Base):
    """노면 점검(관측) 이력 — 지점별로 시간에 따라 쌓인다.

    왜 별도 테이블인가
        「지금 상태」는 ``road/results.py`` 의 메모리에 있지만 **재시작하면
        사라집니다.** 그런데 보수 우선순위를 정하려면 「손상 4건」이 어제도
        4건이었는지 0건에서 늘어난 것인지를 알아야 합니다. 최신 1건만으로는
        구분되지 않습니다.

    ``events`` 로는 대신할 수 없다
        이벤트는 **손상이 잡혔을 때만** 생깁니다. 「봤는데 아무것도 없었다」와
        「분석에 실패했다」는 남지 않는데, 점검 이력에서는 그 둘이 핵심입니다 —
        프레임 0장인 관측을 「이상 없음」으로 오해하면 못 본 구간을 점검 완료로
        처리하게 됩니다.
    """

    __tablename__ = "road_inspections"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 카메라가 지워져도 이력은 남아야 하므로 FK 를 걸지 않고 이름을 복제한다.
    camera_name: Mapped[str] = mapped_column(String(120), default="")
    # 1~4 등급. 분석에 실패했으면 NULL — 0 으로 두면 「정상」으로 읽힌다.
    grade: Mapped[int | None] = mapped_column()
    defect_count: Mapped[int] = mapped_column(default=0)
    frames_analyzed: Mapped[int] = mapped_column(default=0)
    # 프레임 0장이면 「분석한 것이 아니다」. 등급과 별개로 명시한다.
    failed: Mapped[bool] = mapped_column(default=False)
    # continuous(상시 순회) / focus(집중 감시) / manual(사람이 실행)
    source: Mapped[str] = mapped_column(String(24), default="")
    # 100m 당 손상 건수. 구간 길이를 보정한 지점에서만 값이 있고, 아니면 NULL.
    #
    # **관측 당시의 값을 그대로 저장한다.** 조회할 때 다시 계산하면, 나중에
    # 구간 길이를 고쳤을 때 **과거 기록의 의미가 조용히 바뀐다.** 이력은
    # 「그때 무엇이 사실이었나」이므로 계산 결과를 박아 둔다.
    per_100m: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_now, index=True)

    __table_args__ = (
        # 화면은 언제나 「이 지점의 최근 N건」을 읽는다.
        Index("ix_road_inspections_camera_time", "camera_id", "analyzed_at"),
    )


class CrowdObservation(Base):
    """인파 관측 이력 — 지점별로 시간에 따라 쌓인다.

    왜 필요한가
        흐름 지표(속도·방향분산·발산도)를 **매 프레임 계산해 놓고 그때그때
        버리고** 있었습니다. 화면에는 「지금」만 보이고, 위험행동이 잡혔을
        때만 이벤트가 남습니다. 그래서 **「평소보다 붐비나」에 답할 수
        없었습니다** — 비교할 어제가 없기 때문입니다.

        ``surge`` 는 이름 그대로 「평소 대비 배수」인데, 그 「평소」가
        **프로세스 메모리의 최근 60건**뿐이라 재시작하면 사라집니다.

    무엇을 위한 준비인가
        시계열 예측(「15분 내 90% 용량」식)은 **과거가 있어야** 가능합니다.
        이 표가 그 입력이 됩니다. 예측 모델을 붙이기 전에 **먼저 쌓아야**
        합니다 — 오늘 시작해야 다음 달에 쓸 수 있습니다.

    ``events`` 로 대신할 수 없다
        이벤트는 **위험행동이 잡혔을 때만** 생깁니다. 「봤는데 평온했다」가
        남지 않으면 평상시 기준선을 만들 수 없고, 기준선이 없으면 급증도
        가려낼 수 없습니다.
    """

    __tablename__ = "crowd_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 카메라가 지워져도 이력은 남아야 하므로 FK 를 걸지 않고 이름을 복제한다.
    camera_name: Mapped[str] = mapped_column(String(120), default="")

    person_count: Mapped[int] = mapped_column(default=0)
    density_index: Mapped[float] = mapped_column(Float, default=0.0)

    # --- 흐름 지표 ---------------------------------------------------
    mean_speed: Mapped[float] = mapped_column(Float, default=0.0)
    # 평소 대비 속도 배수. 1.0 이 평상시다 — 0 을 기본으로 두면 「속도가
    # 0배」라는 뜻이 되어 나중에 통계를 낼 때 판정이 뒤집힌다.
    surge: Mapped[float] = mapped_column(Float, default=1.0)
    dispersion: Mapped[float] = mapped_column(Float, default=0.0)
    # 양수면 중심에서 퍼짐, 음수면 모임.
    divergence: Mapped[float] = mapped_column(Float, default=0.0)

    # --- 판정 --------------------------------------------------------
    risk_code: Mapped[str] = mapped_column(String(32), default="")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[int] = mapped_column(default=0)
    # 그렇게 판정한 근거(「발산도UP」 등). 나중에 「왜 그때 경보가 떴나」를
    # 되짚을 때 이것이 없으면 숫자만 남는다.
    drivers: Mapped[str] = mapped_column(String(200), default="")

    # 관측이 실제로 이뤄졌나. 프레임을 못 받았는데 인원 0 으로 남기면
    # 「사람이 없었다」로 읽힌다 — 노면의 ``failed`` 와 같은 이유다.
    failed: Mapped[bool] = mapped_column(default=False)
    # mock / detector — 모의 데이터로 만든 기준선을 실측과 섞으면 안 된다.
    source: Mapped[str] = mapped_column(String(24), default="")

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_now, index=True)

    __table_args__ = (
        # 화면·예측 모두 「이 지점의 최근 구간」을 읽는다.
        Index("ix_crowd_observations_camera_time", "camera_id", "observed_at"),
    )


class TrafficObservation(Base):
    """교통 관측 이력 — 지점별로 시간에 따라 쌓인다 (2026-08-21 신설).

    왜 필요한가
        flood/traffic 도메인 분리 전까지 교통 판정(강우×정체×정지차량)은
        **매 틱 계산해 놓고 그때그때 버리고** 있었습니다. 이벤트는
        위험할 때만 남으므로 「봤는데 평온했다」가 기록되지 않고, 그러면
        **평상시 기준선을 만들 수 없습니다** — ``crowd_observations`` 를
        만든 것과 같은 이유입니다.

    무엇을 위한 준비인가
        「이 지점은 비가 오면 늘 막힌다」 같은 판단은 과거가 있어야
        가능합니다. 예측 모델을 붙이기 전에 **먼저 쌓아야** 합니다.

    ⚠️ 아직 기록하는 코드는 없습니다
        표만 먼저 만듭니다. 실제 기록은 이벤트 이중화(5단계) 이후
        ``service/event_sync.py`` 에서 붙입니다 — 지금 넣으면 도메인
        분리가 끝나기 전에 뒤섞인 값이 쌓입니다.
    """

    __tablename__ = "traffic_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 카메라가 지워져도 이력은 남아야 하므로 FK 를 걸지 않고 이름을 복제한다.
    camera_name: Mapped[str] = mapped_column(String(120), default="")

    # --- 관측 지표 ---------------------------------------------------
    rain_mm_h: Mapped[float] = mapped_column(Float, default=0.0)
    # 평상시 대비 평균속도 감소율(0~1). 0 이 「감소 없음」이라 기본값이 맞다.
    speed_drop: Mapped[float] = mapped_column(Float, default=0.0)
    queue_len: Mapped[int] = mapped_column(default=0)
    stalled_count: Mapped[int] = mapped_column(default=0)

    # --- 판정 --------------------------------------------------------
    # TWR_* (traffic_weather.knowledge.ontology.TRAFFIC_RISK_CATALOG).
    # ⚠️ hazard_types.code 와는 **다른 네임스페이스**다 — 그쪽은 「무엇이
    #    발생했나(유형)」, 이쪽은 「판정 결과 상태」다. 둘을 같은 칸에
    #    넣으려다 normalize_hazard_code() 가 코드를 대분류로 뭉개는
    #    문제가 있었다(docs/202608210801 5절).
    risk_code: Mapped[str] = mapped_column(String(32), default="")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[int] = mapped_column(default=0)
    # 그렇게 판정한 근거(「강수 강함(20mm/h)」 등).
    drivers: Mapped[str] = mapped_column(String(200), default="")

    # 관측이 실제로 이뤄졌나. 프레임을 못 받았는데 정체 0 으로 남기면
    # 「원활했다」로 읽힌다 — crowd/road 의 ``failed`` 와 같은 이유다.
    failed: Mapped[bool] = mapped_column(default=False)
    # synthetic / hls / rtsp / video — 합성 데이터로 만든 기준선을 실측과
    # 섞으면 안 된다.
    source: Mapped[str] = mapped_column(String(24), default="")

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_now, index=True)

    __table_args__ = (
        # 화면·예측 모두 「이 지점의 최근 구간」을 읽는다.
        Index("ix_traffic_observations_camera_time", "camera_id", "observed_at"),
    )


class LiveDetectionState(Base):
    """카메라별 「지금」 판정 등급 — 다른 프로세스가 즉시 읽기 위한 표
    (API 게이트웨이 Phase 4, 2026-08-31 신설).

    왜 필요한가
        ``road_inspections``·``traffic_observations``·``crowd_observations``는
        모두 **이력**(시간에 따라 쌓인 관측 여러 건)이다. 하지만 상황판
        홈 화면은 "지금 이 카메라가 몇 등급인가" **한 값**만 필요하고,
        Phase 4(침수·교통위험을 별도 프로세스로 분리)부터는 platform-shell이
        그 값을 자기 프로세스 메모리(``RiskStore``)에서 더는 읽을 수
        없다 — 인파(``CrowdObservation``)·노면(``road_inspections``의
        최신 1건, :func:`.road_history.latest_by_camera`)이 이미 쓰고
        있는 것과 같은 문제다.

    왜 이력 표를 그대로 안 쓰나
        침수·교통은 초당 최대 5회 틱을 도는데, 그 이력 표들은 20초
        주기로만 기록된다(``event_sync.py``) — 그 이력에서 "최신 1건"을
        가져와도 최대 20초 지연이 생긴다. 이 표는 **오직 홈 화면 지연을
        줄이기 위한 것**이라 이력을 남기지 않고 한 카메라·도메인당
        **행 하나만** 계속 덮어쓴다(1초 주기 upsert) — 그래서 이력 표들과
        별개다.

    ``domain`` 하나로 침수·교통을 같이 담는 이유
        두 도메인의 홈 요약 함수(``_flood_summary()``/``_traffic_summary()``)가
        읽는 모양이 사실상 같다(카메라별 최신 등급 1개) — 표를 두 개
        만들면 같은 조회 로직을 두 번 베끼게 된다.
    """

    __tablename__ = "live_detection_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # "flood" | "traffic" — 다른 도메인이 늘어나도 이 표를 그대로 재사용할
    # 수 있게 자유 문자열로 둔다(핵심 코드가 아니라 조회 키일 뿐이다).
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    # 관심/주의/경계/심각 중 하나, 또는 판정이 없으면 빈 문자열.
    level: Mapped[str] = mapped_column(String(8), default="")
    # 이번 틱에 실제로 판정이 돌았는가(=water_available/traffic_enabled에
    # 해당하는 각 도메인의 개념). False면 화면이 "관측 없음"으로 걸러야
    # 하는 카메라라는 뜻 — 등급이 있어도 신뢰하면 안 된다.
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now,
                                                 index=True)

    __table_args__ = (
        # 도메인별 최신 1건만 있으면 되므로 (카메라, 도메인) 조합은 유일하다
        # — 매 upsert가 새 행을 쌓는 게 아니라 이 하나를 계속 덮어쓴다.
        UniqueConstraint("camera_id", "domain",
                         name="uq_live_detection_state_camera_domain"),
    )


class ErrorCode(Base):
    """오류 코드 사전 — 코드 하나에 원인과 해결방법을 붙여 둔 표 (S-92).

    왜 발생 이력과 별도 테이블인가
        같은 오류는 계속 반복됩니다. 「CCTV 스트림 접속 실패」의 원인과 조치는
        발생할 때마다 같은데, 이력 행마다 적어 두면 **조치 방법이 축적되지
        않습니다.** 담당자가 한 번 알아낸 해결책을 코드에 적어 두면, 다음에 같은
        오류를 만난 사람이 이력에서 바로 그 설명을 봅니다.

    ``builtin`` 이 하는 일
        제품이 기본 제공하는 코드는 삭제를 막습니다. 지워 버리면 그 코드로
        기록되던 오류가 「미등록 코드」가 되어 설명 없이 쌓입니다. 대신
        ``is_active`` 로 목록에서 감출 수는 있습니다.
    """

    __tablename__ = "error_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # UG-CCTV-001 형태. 화면·로그·문의 응대에서 이 문자열 하나로 소통한다.
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="SYS")
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    # info / warn / error / critical
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="error")
    cause: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 제품 기본 제공 코드. 삭제 불가, 내용 수정은 가능(현장 조치법을 덧붙이도록).
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now,
                                                 nullable=False)


Index("ix_error_codes_category", ErrorCode.category)


class ErrorLog(Base):
    """실제로 발생한 오류 1건 — 정확히는 **같은 오류의 묶음** 1건 (S-92).

    왜 발생마다 한 행이 아닌가
        CCTV 재접속 실패나 DNS 장애는 **초당 수십 번** 납니다. 발생마다 행을
        만들면 하룻밤에 수십만 행이 쌓여, 정작 중요한 오류 한 건이 그 속에
        묻힙니다. 그래서 같은 오류(:attr:`fingerprint`)는 한 행으로 합치고
        ``count`` 만 올립니다. 「몇 번, 언제부터 언제까지」가 남으므로 정보는
        오히려 더 잘 보입니다.

    ``code`` 에 외래키를 걸지 않는 이유
        두 가지입니다. 첫째, 사전에 **없는 코드로도 기록돼야** 합니다 — 처음 보는
        오류를 「등록된 코드가 아니라서」 버리면 그 오류는 영원히 안 보입니다.
        둘째, 사전에서 코드를 지워도 지난 이력은 남아야 합니다.
    """

    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, default="",
                                      index=True)
    # 같은 오류를 묶는 열쇠 (코드 + 경로 + 정규화한 메시지의 해시).
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="",
                                             index=True)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="error")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 스택트레이스 등. 화면에서는 펼쳐야 보인다.
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # web(화면·API) / worker(상시 탐지 워처) / pipeline(분석) / manual
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="web")
    path: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    method: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    status_code: Mapped[int | None] = mapped_column(Integer)
    # 발생 당시 로그인 사용자. 계정이 지워져도 남도록 문자열로 복제한다.
    login_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                    default=_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   default=_now, nullable=False,
                                                   index=True)

    # 조치 기록. 「해결방법」이 코드 사전의 일반론이라면 이쪽은 이번 건의 실제 조치다.
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolved_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolve_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        # 목록은 「최근 순」이 기본, 검색은 코드·처리여부로 좁힌다.
        Index("ix_error_logs_code_time", "code", "last_seen_at"),
        Index("ix_error_logs_resolved_time", "resolved", "last_seen_at"),
        # 집계 대상을 찾을 때 쓴다 — 같은 지문의 **미처리** 행 1개.
        Index("ix_error_logs_fp_resolved", "fingerprint", "resolved"),
    )


class SopStep(Base):
    """디지털 SOP 한 단계 (S-86 편집 · S-03 이행).

    **왜 있는가.** 지금 화면은 「경계입니다」까지만 말하고, 그래서 무엇을 해야
    하는지는 근무자의 기억에 맡긴다. 야간에 혼자 근무하는 상황실에서 그 기억은
    믿을 것이 못 된다. 기관 매뉴얼의 조치 순서를 여기 옮겨 두면 화면이 대신
    말해 준다.

    ⚠️ **제품이 심는 기본 단계는 기관 매뉴얼이 아니다.** 지자체마다 통제 기준과
    통보 계통이 달라 우리가 정할 수 없다. 기본값은 「무엇을 적어야 하는지」를
    보여 주는 뼈대이며, 도입 기관의 행동매뉴얼로 반드시 교체해야 한다
    (:data:`~.sop.DEFAULT_STEPS` 주석 참고).

    ``domain``·``level`` 이 빈 문자열이면 **전체**를 뜻한다 — 도메인과 무관하게
    모든 이벤트에 붙는 단계(예: 「상황 일지 기록」)를 표현하기 위해서다.
    """

    __tablename__ = "sop_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 필수 단계는 종결 전에 다 찍혀야 한다. 화면이 막지는 않고 **경고**만
    # 한다 — 현장에서 규정대로 못 하는 상황이 실제로 있고, 막으면 사람은
    # 아무 칸이나 찍고 넘어간다.
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 제품이 심은 뼈대. 지울 수는 있지만 화면에서 「기본안」으로 표시해
    # 기관 매뉴얼로 바꿨는지 한눈에 보이게 한다.
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")


Index("ix_sop_scope", SopStep.domain, SopStep.level, SopStep.seq)


class EventSopCheck(Base):
    """이벤트 × SOP 단계 이행 기록.

    행이 있으면 이행(또는 「해당 없음」), 없으면 미이행이다. 미이행 행을 미리
    만들어 두지 않는 이유는 단계 정의가 나중에 바뀌기 때문이다 — 이벤트가 열린
    뒤 단계가 추가돼도 체크리스트에 그대로 나타나야 한다.

    :class:`EventAction` 과 같은 원칙으로 **감사 대응의 근거라 수정하지 않는다.**
    잘못 찍었으면 해제하고 다시 찍되, 해제 사실도 조치 이력에 남긴다.
    """

    __tablename__ = "event_sop_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    # 단계가 지워져도 「무엇을 했는지」는 남아야 하므로 제목을 함께 적어 둔다.
    step_id: Mapped[int | None] = mapped_column(
        ForeignKey("sop_steps.id", ondelete="SET NULL"))
    step_title: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    login_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 「해당 없음」 처리. 사유 없이는 넘어갈 수 없게 화면에서 막는다.
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)

    __table_args__ = (UniqueConstraint("event_id", "step_id",
                                       name="uq_event_sop_step"),)


class ShiftHandover(Base):
    """교대 인수인계 (S-04).

    **왜 있는가.** 관제는 24시간 이어지는데 사람은 8~12시간마다 바뀐다. 지금은
    그 이음매에 아무것도 없어서, 「밤에 무슨 일이 있었는지」가 말과 수첩으로만
    넘어간다. 야간에 열려 있던 이벤트가 주간 근무자에게 전달되지 않으면 그
    이벤트는 아무도 안 보는 채로 남는다.

    ``open_events`` 는 **작성 시점의 스냅샷**이다. 지금 열린 이벤트를 그때그때
    조회하지 않고 굳혀 두는 이유는, 나중에 이벤트가 종결돼도 「인계 시점에
    무엇이 열려 있었는가」가 남아야 하기 때문이다. 사후 검토는 그 시점의
    사실을 묻지, 지금의 사실을 묻지 않는다.

    :class:`AuditLog` 처럼 지우지 않는다. 인계 기록을 지울 수 있으면 「전달
    못 받았다」는 다툼에 답할 근거가 사라진다.
    """

    __tablename__ = "shift_handovers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 근무일과 근무조 이름. 조 편성(2교대·3교대·명칭)은 기관마다 달라
    # 목록으로 고정하지 않고 문자열로 받는다.
    shift_date: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    shift_name: Mapped[str] = mapped_column(String(32), nullable=False,
                                            default="")

    # 인계자(넘기는 사람). 계정이 지워져도 기록은 남아야 하므로 로그인 ID를
    # 함께 적어 둔다.
    from_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    from_login: Mapped[str] = mapped_column(String(64), nullable=False,
                                            default="")
    # 인수자(받는 사람). 작성 시점에 비어 있을 수 있다 — 다음 근무자가
    # 확정되지 않은 채 인계문을 먼저 쓰는 일이 흔하다.
    to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    to_login: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 다음 근무조가 반드시 해야 할 일. 요약과 나눈 이유는, 섞어 쓰면 읽는
    # 사람이 「내가 할 일」을 못 찾기 때문이다.
    todo: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 작성 시점의 미처리·처리중 이벤트 스냅샷.
    open_events: Mapped[dict | None] = mapped_column(JSONB)

    # draft(작성중) / submitted(인계함) / acknowledged(인수 확인)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)


Index("ix_handover_date", ShiftHandover.shift_date.desc())
Index("ix_handover_status", ShiftHandover.status)


class EventEvidence(Base):
    """탐지 이벤트의 증거 자료 (S-88).

    이벤트 하나에 정지영상 1건과 클립 1건이 붙는 것이 보통이지만, 등급이
    올라 다시 수집하면 여러 건이 될 수 있다. 그래서 이벤트당 **여러 행**을
    허용한다.

    ⚠️ **파일 자체가 개인정보다.** 사람과 차량이 찍힌다. 열람·내려받기·삭제를
    모두 권한으로 막고 감사 로그에 남긴다. 밖으로 내주면 영상 반출
    관리대장(:class:`VideoDisclosure`)에도 적어야 한다.

    ``sha256`` 을 남기는 이유 — 증거 자료는 **「제출한 파일이 그때 그 파일이
    맞다」** 를 말할 수 있어야 한다. 파일을 바꿔치기하면 해시가 달라진다.
    """

    __tablename__ = "event_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 이벤트를 지우면 증거도 함께 지운다 — 근거 없는 파일만 남으면 그것이
    # 곧 목적 없는 개인정보 보관이 된다.
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    domain: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # 수집 당시 등급. 이벤트 등급은 나중에 바뀌므로 **그때 값을 굳혀 둔다** —
    # 「어느 등급이라 남겼는가」를 나중에 알 수 없으면 설정 점검이 안 된다.
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    kind: Mapped[str] = mapped_column(String(16), nullable=False)   # image / clip
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 클립 길이(초). 정지영상은 0.
    duration_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=_now, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ★ 이벤트가 난 위치 (2026-08-20, S-88 팝업 요구사항).
    #
    #   **탐지 상자(bbox)는 지어낼 수 없다** — 실제로 관측된 값만 담는다.
    #   도메인마다 「실제 관측」의 의미가 다르다.
    #
    #   * 침수 — 물로 덮인 것으로 **세그멘테이션이 판정한 영역**의 경계
    #   * 인파 — **배회·침입을 일으킨 그 사람의 추적 상자**
    #   * 노면 — **YOLO 가 실제로 찾은 손상 상자**
    #
    #   ⚠️ 못 구한 경우가 있다 — 예: 침수인데 그 틱에 물 픽셀이 없었다,
    #   인파인데 밀집도만으로 뜬 이벤트라 특정 사람이 없다. 그럴 때는
    #   **빈 배열([])이지 자리채움이 아니다.** 화면은 「없다」고 말해야 하므로
    #   `nullable=True` 로 두고, 값이 없으면 `None` 이다(빈 리스트와 구분).
    boxes: Mapped[list | None] = mapped_column(JSONB)
    # boxes 좌표가 기준으로 삼는 원본 프레임 크기(px). ROI 의 프레임 크기와
    # **다를 수 있다**(다른 시점에 잡힌 프레임이라 해상도가 바뀌었을 수 있음) —
    # 그래서 ROI 와 별도로 자신의 크기를 갖고 다닌다.
    frame_w: Mapped[int | None] = mapped_column(Integer)
    frame_h: Mapped[int | None] = mapped_column(Integer)


Index("ix_evidence_event", EventEvidence.event_id)
Index("ix_evidence_level", EventEvidence.level)
Index("ix_evidence_captured", EventEvidence.captured_at.desc())


# ---------------------------------------------------------------------------
# 관계 모델 (Urban Ontology 1단계) — docs/202608181432/relation_model_design.md
#
# **왜 있는가.** 확보한 제안요청서 3건이 「관계」를 요구한다. 경남 SFR-005 는
# 「센서의 물리적 위치와 인근 CCTV 를 1:1 또는 N:1 로 매핑」을, 기대효과 절은
# 「시·군 경계를 넘나드는 산불 확산·하천 범람을 연속 추적」을 요구한다.
# 지금 우리에게는 카메라 사이·카메라와 센서 사이 관계가 아예 없다.
#
# ⚠️ **기존 문자열 키를 건드리지 않는다.** ``cameras.id`` 가
# ``BLOCK-CHORYANG`` 같은 문자열이고 ``events.block_id`` 와 ROI 파일명이 거기
# 묶여 있다. 아래는 전부 **덧붙이는** 표이며, 비어 있어도 기존 동작이 그대로다.
# ---------------------------------------------------------------------------


class RiskLevel(Base):
    """위험등급 어휘.

    **왜 표로 빼는가.** 지금 「관심·주의·경계·심각」이 ``core/calibration.py``
    에 문자열로 박혀 있고 ``roles.py`` 에 ``CRITICAL_LEVELS`` 집합이 따로 있다.
    경남 제안요청서 SFR-012 는 「**최소 4단계 이상**」이라 적었다 — 5단계를 쓰는
    기관이 오면 지금 구조로는 **코드를 고쳐야** 한다.

    ⚠️ **기존 ``events.level`` 문자열은 그대로 둔다.** 이 표는 우선 「설명하는
    표」로 시작한다. 한 번에 갈아치우면 지난 이벤트 이력이 끊긴다.

    ``seq`` 가 정렬과 비교의 유일한 기준이다. 이름으로 크기를 비교하면
    기관이 등급 이름을 바꾸는 순간 판정이 뒤집힌다.
    """

    __tablename__ = "risk_levels"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    # ★ 등급의 **성격**. `risk`(위험) / `maintenance`(정비).
    #
    #   왜 필요한가 — 노면은 「양호·관찰·보수 필요·긴급」인데 이것은 **위험
    #   등급이 아니라 정비 등급**이다. 「긴급」은 「지금 통제하라」가 아니라
    #   **「빨리 보수하라」**다. 같은 자리에서 섞어 세면 상황판에서 침수
    #   「심각」과 노면 「긴급」 중 무엇이 더 급한지 알 수 없게 된다.
    #
    #   ⚠️ 그래서 이 칸이 `maintenance` 인 행은 **이벤트 등급 비교·경보
    #   판정에 들어가지 않는다**(vocabulary 의 조회 함수들이 걸러낸다).
    #   구간 숫자를 화면에서 바꾸게 하려고 같은 표에 담되, **쓰이는 자리는
    #   분리한다.**
    kind: Mapped[str] = mapped_column(String(16), nullable=False,
                                      default="risk")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # 단독 발송이 허용되는 등급인가 — roles.CRITICAL_LEVELS 를 대체할 자리.
    is_critical: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                              default=False)
    # 표준 어휘 매핑용. **지금은 비워 둔다** — 경남 스마트시티 데이터허브가
    # 어떤 표준을 쓰는지 제안요청서 원문에 없다. 표준이 정해지면 행만 채운다.
    std_uri: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class HazardType(Base):
    """위험유형 어휘.

    ``events.event_type`` 이 ``String(32)`` 인데 **무엇이 올 수 있는지 어디에도
    적혀 있지 않다.** 이 표가 그 목록이다.

    ``parent_code`` 로 계층을 만든다 — ``flood`` 아래 ``flood_underpass``.
    화면에서 「침수 전체」로 묶어 보려면 상위 코드가 필요하다.

    ``source`` 는 그 어휘가 어디서 왔는지다. ``rfp`` 로 표시된 유형은 **발주
    요구에서 그대로 가져온 것**이라 제안서에서 근거를 댈 수 있다.

    ⚠️ ``detectable`` 을 따로 두는 이유 — **어휘 등록과 탐지 가능은 다르다.**
    산불·태풍은 어휘로 등록하되 ``detectable=False`` 다. 이 구분이 없으면
    「우리는 산불도 탐지합니다」라는 거짓말이 화면에서 만들어진다.
    """

    __tablename__ = "hazard_types"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 기존 roles.Domain(flood/crowd/road)과 잇는 칸. 새 유형은 비어 있을 수 있다.
    domain: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    parent_code: Mapped[str | None] = mapped_column(
        ForeignKey("hazard_types.code", ondelete="SET NULL"))
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    # own(자체) / national(국가표준) / rfp(발주요구)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="own")
    detectable: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                             default=False)
    std_uri: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Zone(Base):
    """구역 — 행정구역과 위험구역을 같은 표에 둔다.

    나누지 않는 이유는 **카메라가 둘 다에 속하기** 때문이다. 초량 CCTV 는
    「부산 동구」에 속하면서 동시에 「침수흔적 구역」에 속한다. 표를 나누면
    카메라-구역 관계표도 둘로 나뉜다.

    ``path`` 는 점으로 이은 계층 경로다(``kr.gyeongnam.changwon``).

    ⚠️ **설계서와 달리 ``ltree`` 확장을 쓰지 않는다.** ``varchar`` +
    ``LIKE 'prefix.%'`` 로 같은 일이 되고 **추가 설치가 0** 이 된다. 경남이
    도 + 18개 시·군이라 행 수가 수백 단위라 성능 차이가 없다. 깊이가 깊어지거나
    실측이 느려지면 그때 ``ltree`` 로 바꾼다.

    ⚠️ **도형(폴리곤)은 넣지 않는다.** PostGIS 가 설치본에 없다. 지금은 구역의
    **존재와 계층**만 다룬다.
    """

    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # admin(행정) / hazard(위험) / custom
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")
    path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    hazard_type_code: Mapped[str | None] = mapped_column(
        ForeignKey("hazard_types.code", ondelete="SET NULL"))
    # 침수흔적도·인명피해우려지역 등 출처. 어디서 온 구역인지 모르면
    # 나중에 갱신할 수 없다.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Sensor(Base):
    """외부 센서 — 수위계·우량계·IoT.

    근거는 경남 SFR-005 「기존 센서 및 IoT 연동」이다. 기상청 API 모듈이 이미
    있으므로 첫 행은 거기서 채울 수 있다.

    ``last_seen_at`` 을 두는 이유 — **끊긴 센서를 식별해야** 한다. 값이 안 오는
    센서를 「이상 없음」으로 읽으면 위험을 놓친다.
    """

    __tablename__ = "sensors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # water_level / rain_gauge / iot / other
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    # kma(기상청) / local / manual
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    external_id: Mapped[str] = mapped_column(String(128), nullable=False,
                                             default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CameraLink(Base):
    """카메라 ↔ 카메라 관계.

    **이 표가 관계 모델의 핵심이다.** ``kind`` 가 여는 것이 서로 다르다.

    - ``adjacent`` — 인접. 알람 발생 시 **주변 카메라를 함께 띄운다**
      (202608172039 조사에서 KT GiGAeyes 에는 있고 우리엔 없던 기능)
    - ``upstream`` / ``downstream`` — 물길의 위·아래.
      **상류가 차오르면 하류를 미리 경고할 수 있다.** 지금 우리가 못 하는 일이고,
      경남 기대효과의 「연속 추적」이 실제로 요구하는 것이다
    - ``overlap`` — 시야가 겹침. 같은 사건이 두 번 잡히는 것을 막는 데 쓴다

    ``auto`` 를 두는 이유 — **좌표로 자동 생성한 것과 사람이 정한 것을 섞으면
    안 된다.** 자동 생성을 다시 돌릴 때 사람이 넣은 상·하류 관계까지 지워지면
    복구할 방법이 없다. 상·하류는 좌표만으로 알 수 없어 반드시 사람이 넣는다.

    방향이 있는 관계다 — ``adjacent`` 는 양쪽에 행을 만들고, ``upstream`` 은
    한쪽만 만든다. A 의 상류가 B 면 B 의 상류는 A 가 아니다.
    """

    __tablename__ = "camera_links"
    __table_args__ = (
        UniqueConstraint("from_camera_id", "to_camera_id", "kind",
                         name="uq_camera_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    to_camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False,
                                      default="adjacent")
    distance_m: Mapped[int | None] = mapped_column(Integer)
    # from → to 방위(0~359). 자동 생성 시 좌표에서 계산한다.
    bearing_deg: Mapped[int | None] = mapped_column(Integer)
    auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)


class CameraSensor(Base):
    """카메라 ↔ 센서 관계.

    경남 SFR-005 원문이 그대로 설계다.

        각 센서의 물리적 위치 정보와 **인근에 설치된 CCTV 카메라를 1:1 또는
        N:1 로 매핑**하여, 센서 이벤트 발생 시 **연관된 카메라 영상을 즉시 호출**

    그래서 유일 제약을 (카메라, 센서) 쌍에만 건다 — **센서 하나에 카메라 여럿**이
    붙을 수 있어야 한다.

    ``role`` 로 대표 센서를 구분한다. 참고 센서까지 전부 화면에 띄우면
    관제요원이 무엇을 봐야 할지 알 수 없다.
    """

    __tablename__ = "camera_sensors"
    __table_args__ = (
        UniqueConstraint("camera_id", "sensor_id", name="uq_camera_sensor"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    sensor_id: Mapped[str] = mapped_column(
        ForeignKey("sensors.id", ondelete="CASCADE"), nullable=False)
    # primary(대표) / reference(참고)
    role: Mapped[str] = mapped_column(String(16), nullable=False,
                                      default="reference")
    distance_m: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class CameraZone(Base):
    """카메라 ↔ 구역 관계.

    ⚠️ **기존 ``cameras.sido``·``sigungu`` 문자열을 지우지 않는다.** 화면과
    엑셀 내려받기가 그 값을 쓰고 있다. 이 표는 **위험구역 관계부터** 채우고,
    행정구역은 나중에 옮긴다.

    ``coverage`` 가 필요한 이유 — 사각지대 분석(S-63)에서 「구역 가장자리만
    걸친 카메라」를 「구역 전체를 보는 카메라」와 같이 세면 커버리지가 부풀려진다.
    """

    __tablename__ = "camera_zones"
    __table_args__ = (
        UniqueConstraint("camera_id", "zone_id", name="uq_camera_zone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False)
    zone_id: Mapped[str] = mapped_column(
        ForeignKey("zones.id", ondelete="CASCADE"), nullable=False)
    # full / partial / edge
    coverage: Mapped[str] = mapped_column(String(16), nullable=False,
                                          default="partial")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class HazardSopMap(Base):
    """위험유형 × 등급 (× 구역) → SOP 단계.

    근거는 경남 SFR-012 「단계별 SOP 를 자동으로 **도, 시·군별로** 제시」다.
    ``zone_id`` 칸이 그 「도, 시·군별」이다.

    ⚠️ **기존 ``sop_steps.domain``·``level`` 매칭을 대체하지 않는다.** 이 표는
    **더 정밀한 매칭이 필요할 때만 우선 적용**되는 덧표다. 비어 있으면 지금과
    똑같이 동작한다 — 그래서 회귀 위험이 없다.

    ``level_code``·``zone_id`` 가 빈 문자열이면 「전체」다. NULL 을 쓰지 않는
    이유는 유일 제약이 NULL 을 서로 다른 값으로 봐서 중복이 새기 때문이다.
    """

    __tablename__ = "hazard_sop_map"
    __table_args__ = (
        UniqueConstraint("hazard_type_code", "level_code", "zone_id",
                         "sop_step_id", name="uq_hazard_sop"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hazard_type_code: Mapped[str] = mapped_column(
        ForeignKey("hazard_types.code", ondelete="CASCADE"), nullable=False)
    level_code: Mapped[str] = mapped_column(String(16), nullable=False,
                                            default="")
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sop_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("sop_steps.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


Index("ix_hazard_types_domain", HazardType.domain)
Index("ix_hazard_types_parent", HazardType.parent_code)
Index("ix_zones_path", Zone.path)
Index("ix_zones_kind", Zone.kind)
Index("ix_camera_links_from", CameraLink.from_camera_id, CameraLink.kind)
Index("ix_camera_links_to", CameraLink.to_camera_id)
Index("ix_camera_sensors_camera", CameraSensor.camera_id)
Index("ix_camera_sensors_sensor", CameraSensor.sensor_id)
Index("ix_camera_zones_zone", CameraZone.zone_id)
Index("ix_hazard_sop_lookup", HazardSopMap.hazard_type_code,
      HazardSopMap.level_code)


class LevelThreshold(Base):
    """도메인별 등급 구간 — 「몇 부터 어느 등급인가」.

    **왜 별도 표인가.** 도메인마다 재는 단위가 다르다 — 침수는 cm, 인파는
    명/㎡, 노면은 건/100m. :class:`RiskLevel` 에 컬럼으로 넣으면 도메인이
    늘 때마다 마이그레이션이 필요하다.

    ``min_value`` 이상이면 그 등급이다. 가장 높은 등급부터 내려오며 비교한다.

    ⚠️ **비어 있으면 코드 기본값을 쓴다.** 표를 비웠다고 「구간이 없다」로 읽으면
    전 지점이 등급 없이 뜬다 — 관제 화면이 통째로 무의미해진다
    (:mod:`~.calibration` 참고).

    기본값의 출처는 외부 기준이다(``calibration`` 머리말) — 침수 5cm 는 행안부
    지하차도 통제 기준, 15cm 는 차량 접지력 상실(NWS/FEMA), 30cm 는 소형차
    부유다. **기관이 바꿀 수 있게** 표로 뺐지만 근거는 화면에 남긴다.
    """

    __tablename__ = "level_thresholds"
    __table_args__ = (
        UniqueConstraint("domain", "level_code", name="uq_level_threshold"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    level_code: Mapped[str] = mapped_column(
        ForeignKey("risk_levels.code", ondelete="CASCADE"), nullable=False)
    min_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # 이 숫자가 어디서 왔는지. 기관이 바꿔도 원래 근거는 남아야 한다.
    source_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")


Index("ix_level_thresholds_domain", LevelThreshold.domain)


# ---------------------------------------------------------------------------
# 탐지 피드백 (S-07) — docs/202608181305/menu_structure_plan.md
#
# **왜 있는가.** 세 제안요청서가 정면으로 요구한다.
#   경남 SFR-003 「조치 결과(오탐, 실제 상황, 조치 완료)를 입력하여 **학습
#                 데이터로 활용**」
#   서울 SFR-009 「관제시스템 내 **미탐/오탐 보정** 기능 제공」
#   서울 SFR-015 「**관제 피드백 기반 AI 성능 고도화**」
#
# 우리는 노면 학습 프레임을 모으지만 **관제요원의 판정이 학습으로 되돌아가는
# 경로가 없었다.** 이 고리가 있어야 「지속 학습」을 제안서에 쓸 수 있다.
# ---------------------------------------------------------------------------

# 판정값. ``unclear`` 를 두는 이유 — 억지로 둘 중 하나를 고르게 하면 사람은
# 아무거나 찍는다. 「모르겠다」가 정직한 답인 경우가 실제로 있다.
VERDICT_TRUE = "true_positive"      # 실제 상황이었다
VERDICT_FALSE = "false_positive"    # 오탐이었다
VERDICT_UNCLEAR = "unclear"         # 영상만으로는 판단 못 함
VERDICT_MISSED = "false_negative"   # 미탐 — 있었는데 탐지가 못 잡았다


class DetectionFeedback(Base):
    """탐지 판정 — 정탐·오탐·판단보류·미탐.

    ⚠️ **이벤트가 없는 판정이 있다.** 미탐(``false_negative``)은 「탐지가 못
    잡았다」는 뜻이라 붙일 이벤트가 없다. 그래서 ``event_id`` 가 NULL 이고,
    대신 지점·시각·유형을 사람이 적는다.

    ⚠️ **고쳐 쓰지 않고 새로 남긴다.** 판정이 바뀌면 새 행을 쌓는다 —
    「처음엔 오탐이라 했다가 나중에 정탐으로 바꿨다」는 사실 자체가
    모델 평가에 필요한 정보다(:class:`EventAction` 과 같은 원칙).

    ``used_for_training`` 은 **학습에 실제로 쓰였는지**다. 판정했다고 자동으로
    학습되지 않는다 — 그 사이에 사람이 라벨을 확인하는 단계가 있다.
    """

    __tablename__ = "detection_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 미탐이면 NULL. 이벤트가 지워져도 판정 기록은 남아야 하므로 SET NULL.
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"))
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    domain: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    hazard_type_code: Mapped[str] = mapped_column(String(32), nullable=False,
                                                  default="")
    # 판정 당시 이벤트 등급. 이벤트 등급은 나중에 바뀌므로 **그때 값을 굳힌다**.
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    # 왜 그렇게 봤는지. 오탐이면 무엇을 잘못 봤는지가 모델 개선의 알맹이다.
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 미탐일 때 「언제 있었나」. 정탐·오탐은 이벤트 시각을 쓴다.
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    login_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)

    used_for_training: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                    default=False)


Index("ix_feedback_event", DetectionFeedback.event_id)
Index("ix_feedback_camera", DetectionFeedback.camera_id)
Index("ix_feedback_created", DetectionFeedback.created_at.desc())
Index("ix_feedback_verdict", DetectionFeedback.verdict)


class MultiviewLayout(Base):
    """멀티뷰 화면 배치 (S-05).

    근거는 경남 SFR-001 「관제 요원의 편의에 따라 **화면 분할, 이벤트 목록창
    배치 등 레이아웃을 자유롭게 구성하고 저장**할 수 있어야 한다」이다.

    **사용자마다 다르다.** 야간 근무자와 주간 근무자가 보는 지점이 다르고,
    한 사람이 상황에 따라 여러 배치를 쓴다. 그래서 (사용자, 이름) 단위다.

    ``cameras`` 는 칸 순서대로 담은 지점 ID 목록이다. 빈 칸은 빈 문자열로
    자리를 지킨다 — 목록을 당겨 버리면 사용자가 정한 배치가 무너진다.
    """

    __tablename__ = "multiview_layouts"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_multiview_layout"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 분할 수 (4 / 9 / 16). 열 수가 아니라 칸 수다.
    tiles: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    cameras: Mapped[list | None] = mapped_column(JSONB)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                             default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)


Index("ix_multiview_user", MultiviewLayout.user_id)


class UserPref(Base):
    """사용자별 화면 설정 (키·값).

    **왜 기관 설정(:class:`AppSetting`)과 따로 두는가.** 기관 설정은 기관이
    정하는 값(기관명, 임계값)이고, 여기 있는 것은 **사람마다 다른 값**이다.
    야간 근무자와 주간 근무자가 같은 화면을 다르게 쓴다.

    **왜 브라우저(localStorage)가 아닌가.** 관제요원은 자리를 옮겨 앉는다.
    브라우저에 두면 옆자리 PC 로 가는 순간 설정이 사라지고, 본인은 **껐다고
    생각한 것이 켜져 있는** 상태가 된다.

    ⚠️ **여기에 개인정보를 넣지 않는다.** 화면 설정만 담는 자리다.
    """

    __tablename__ = "user_prefs"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_user_pref"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)


Index("ix_user_prefs_user", UserPref.user_id)


class TrainingRun(Base):
    """AI 모델 학습 실행 기록 (신규 — 2026-08-25).

    ``docs/pending_tasks.md`` A-7(★★ 「재학습·MLOps — 성능 기록 표가 아예
    없음」)에서 지적된 그 표다. 지금까지 학습 결과(F1·mAP50 등)는 전부
    문서(``docs/*.md``)에 손으로 적었다 — 화면에서 재학습을 실행하는 이
    기능을 만들며, 그 결과를 DB에 남기는 것이 자연스러운 자리다.

    4개 탐지 도메인이 학습 스크립트·평가 지표가 전부 다르므로(세그멘테이션
    F1/IoU vs 객체검출 mAP50/precision/recall), ``metrics``를 JSONB로 두고
    도메인마다 다른 키를 담는다 — 공통 컬럼으로 강제하면 도메인마다 안 쓰는
    컬럼이 늘어난다.
    """

    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    # 학습 스크립트가 실제로 쓴 인자. 재현·감사용으로 그대로 남긴다.
    params: Mapped[dict | None] = mapped_column(JSONB)
    # queued | running | succeeded | failed | stopped
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    pid: Mapped[int | None] = mapped_column(Integer)
    log_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    output_dir: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # 도메인별로 키가 다르다(예: 침수={"val_f1":..,"val_iou":..}, 노면={"mAP50":..}).
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    started_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    started_by_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_training_runs_domain", TrainingRun.domain)
Index("ix_training_runs_status", TrainingRun.status)
Index("ix_training_runs_started", TrainingRun.started_at.desc())
