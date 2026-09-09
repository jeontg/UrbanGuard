"""과거 이벤트의 `hazard_type_code` 를 도메인에서 채운다

**왜 지금 하는가.** 이 칸을 **채우는 곳이 없어** 모든 이벤트가 비어 있었다
(2026-08-19 확인 — 31건 전부). 유형 어휘를 만들어 두고 이벤트가 그것을
가리키지 않으면, 유형 기준 통계·필터·SOP 연결이 전부 빈손이 된다.

⚠️ **채우는 것은 「대분류」뿐이다.** 도메인에서 도출할 수 있는 것이 거기까지다.
「지하차도 침수」인지 「배수로 침수」인지는 **지나간 이벤트로는 알 수 없다.**
아는 척해서 세분류를 찍으면, 그 값으로 갈리는 SOP 가 틀린 절차를 안내한다.

⚠️ **비어 있는 것만 채운다.** 이미 값이 있으면 건드리지 않는다 — 사람이나
탐지기가 넣은 세분류를 대분류로 덮으면 정보가 줄어든다.

## downgrade 를 비워 둔 이유

되돌리려면 「이 마이그레이션이 채운 것」과 「그 뒤에 새로 들어온 것」을
구분해야 하는데, **구분할 근거가 없다.** 전부 비우면 새 이벤트의 값까지
지운다. 채워 넣은 값은 도메인에서 언제든 다시 도출할 수 있으므로,
**되돌리지 않는 편이 안전하다.**

Revision ID: b3e5f1a72c04
Revises: a1c7d90e4b52
Create Date: 2026-08-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b3e5f1a72c04"
down_revision = "a1c7d90e4b52"
branch_labels = None
depends_on = None

# 도메인 → 대분류 위험유형. 원본은 core/vocabulary.BASE_HAZARD_BY_DOMAIN 이며,
# 마이그레이션은 **패키지 임포트 없이** 돌아야 해서(스키마가 코드보다 앞설 수
# 있다) 값만 복제한다. 한쪽을 고치면 다른 쪽도 고쳐야 한다.
_BASE = {"flood": "flood", "crowd": "crowd", "road": "road"}


def upgrade() -> None:
    conn = op.get_bind()
    for domain, code in _BASE.items():
        conn.execute(sa.text(
            "UPDATE events SET hazard_type_code = :code "
            "WHERE domain = :domain "
            "  AND (hazard_type_code IS NULL OR hazard_type_code = '')"
        ), {"code": code, "domain": domain})


def downgrade() -> None:
    # 위 머리말 참고 — 되돌리지 않는다.
    pass
