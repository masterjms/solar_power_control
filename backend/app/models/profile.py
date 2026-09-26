"""설정 프로필 — config_profile (docs/03, 사양서 §4.2 "프로필 + 단말 예외").

`ti`/`ka` 를 단말마다 입력하게 하지 않고 요금제·운영 방식별 공용 값을 고르게 한다.
적용값은 `device.ti_override ?? profile.ti`. 프로필의 값을 바꾸면 그 프로필을 쓰는 모든
단말의 `cv_server` 를 +1 해 각 단말의 다음 송신 때 CONFIG_SET 이 나가게 한다.

시드(마이그레이션 0002): 1 "1,100원 시험" 600/300, 2 "1,100원 운영" 1800/600,
3 "2,200원 관제" 300/120. id 1 은 device.profile_id 의 기본값이라 지울 수 없다.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import CheckConstraint, DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import KA_MAX_SEC, TI_MAX_SEC
from app.models.base import Base

#: device.profile_id 기본값. 마이그레이션 시드의 첫 행.
DEFAULT_PROFILE_ID = 1


class ConfigProfile(Base):
    __tablename__ = "config_profile"
    __table_args__ = (
        CheckConstraint(f"ti BETWEEN 1 AND {TI_MAX_SEC}", name="ck_profile_ti_range"),
        CheckConstraint(f"ka BETWEEN 1 AND {KA_MAX_SEC}", name="ck_profile_ka_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: Telemetry 주기(초) 60~3600.
    ti: Mapped[int] = mapped_column(Integer, nullable=False)
    #: MQTT keepalive(초) 60~1800. 운영은 600 이하 권장(사양서 §1.1.10).
    ka: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
