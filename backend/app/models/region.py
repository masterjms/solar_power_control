"""법정동 트리 — region (docs/03 5차, ADR-005, 사양서 §3.9.3·§3.10.4).

시도 > 시군구 > 법정동(말단). 말단만 `bjd_code` 를 갖고, 그룹 = 말단 하나(group_id =
bjd_code + "00"). group_id 는 저장하지 않고 계산한다 — 확장 2자리를 쓰게 되면 규칙이 바뀌는데
저장해 두면 두 곳을 고쳐야 한다.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Region(Base):
    __tablename__ = "region"
    __table_args__ = (
        CheckConstraint("level IN ('sido','sigungu','dong')", name="ck_region_level"),
        # 말단 ⇔ 법정동코드. 코드 없는 말단은 그룹 topic 을 만들 수 없고, 코드 있는 상위 노드는
        # "상위 = 하위 말단마다 발행" 규칙과 어긋난다.
        CheckConstraint("(level = 'dong') = (bjd_code IS NOT NULL)", name="ck_region_leaf_code"),
        CheckConstraint("bjd_code IS NULL OR bjd_code ~ '^[0-9]{10}$'",
                        name="ck_region_bjd_code_format"),
        # 같은 부모 아래 같은 이름 금지. 최상위(부모 NULL)는 UNIQUE 가 NULL 을 서로 다르게 보므로
        # 부분 인덱스로 따로 막는다.
        Index("ux_region_parent_name", "parent_id", "name", unique=True,
              postgresql_where="parent_id IS NOT NULL"),
        Index("ux_region_root_name", "name", unique=True, postgresql_where="parent_id IS NULL"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("region.id"), index=True
    )
    #: sido / sigungu / dong
    level: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: 법정동코드 10자리. dong 만.
    bjd_code: Mapped[str | None] = mapped_column(CHAR(10), unique=True)
    #: 말단 중심 좌표(카카오). "오늘 밤" 유지시간 계산에 쓴다.
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
