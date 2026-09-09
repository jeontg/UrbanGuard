"""cameras.sido, cameras.sigungu 행정구역

지역을 좌표에서 유추하던 것을 **입력받아 저장**하도록 바꾼다. 사각형으로는
구·군을 가를 수 없기 때문이다.

기존 지점은 좌표로 **시/도만** 채운다. 구·군은 비워 둔다 —
틀린 구 이름이 들어가면 그것을 근거로 한 통계가 전부 어긋난다.

Revision ID: a1c7e94b52d0
Revises: 360aa4ff3b21
Create Date: 2026-08-16 11:02:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1c7e94b52d0'
down_revision: Union[str, Sequence[str], None] = '360aa4ff3b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 시/도별 경위도 사각형. `cctv_sources.REGIONS` 와 같은 값이다. 마이그레이션은
# 애플리케이션 코드를 임포트하지 않는 편이 안전해(나중에 그 모듈이 바뀌어도
# 과거 리비전이 깨지지 않는다) 이 시점의 값을 그대로 박아 둔다.
BBOX = {
    "busan": (128.7, 129.4, 34.9, 35.45),
    "ulsan": (128.9, 129.5, 35.4, 35.8),
    "gyeongnam": (127.5, 129.3, 34.5, 35.9),
    "seoul": (126.76, 127.18, 37.42, 37.70),
    "incheon": (126.37, 126.80, 37.30, 37.65),
    "gyeonggi": (126.30, 127.85, 36.90, 38.30),
    "gangwon": (127.05, 129.37, 37.00, 38.62),
    "chungbuk": (127.25, 128.65, 36.00, 37.25),
    "chungnam": (125.95, 127.60, 35.98, 37.10),
    "daejeon": (127.25, 127.56, 36.18, 36.50),
    "sejong": (127.15, 127.40, 36.42, 36.72),
    "jeonbuk": (126.40, 127.95, 35.35, 36.15),
    "jeonnam": (125.95, 127.90, 33.90, 35.50),
    "gwangju": (126.65, 127.02, 35.05, 35.26),
    "gyeongbuk": (127.80, 129.60, 35.65, 37.55),
    "daegu": (128.35, 128.78, 35.65, 36.02),
    "jeju": (126.10, 126.99, 33.10, 33.60),
}


def _sido_of(lat, lng):
    """드는 사각형 중 **가장 좁은 것**. 서울은 경기 안에, 부산은 경남 안에 든다."""
    if lat is None or lng is None:
        return ""
    hits = [(k, (x2 - x1) * (y2 - y1))
            for k, (x1, x2, y1, y2) in BBOX.items()
            if x1 <= lng <= x2 and y1 <= lat <= y2]
    return min(hits, key=lambda x: x[1])[0] if hits else ""


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("cameras", sa.Column("sido", sa.String(length=32),
                                       nullable=False, server_default=""))
    op.add_column("cameras", sa.Column("sigungu", sa.String(length=64),
                                       nullable=False, server_default=""))

    # 기존 지점의 시/도를 좌표로 채운다.
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, lat, lng FROM cameras WHERE sido = ''")).fetchall()
    for cid, lat, lng in rows:
        sido = _sido_of(lat, lng)
        if sido:
            conn.execute(sa.text("UPDATE cameras SET sido = :s WHERE id = :i"),
                         {"s": sido, "i": cid})

    # 채운 뒤 기본값은 걷어낸다. 앞으로는 애플리케이션이 값을 정한다.
    op.alter_column("cameras", "sido", server_default=None)
    op.alter_column("cameras", "sigungu", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("cameras", "sigungu")
    op.drop_column("cameras", "sido")
