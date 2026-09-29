"""알람(S-24) — alarm (docs/03 "알람", ADR-009).

한 단말·한 항목 열린 알람 1건 = 부분 유니크 `(uuid, kind) WHERE closed_at IS NULL`.
지속 조건 항목은 먼저 관찰 행(opened_at NULL)으로 두었다가 시간이 차면 연다.
단말을 지워도 이력은 남긴다(FK 없음, device_event 와 같다).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import UUID_LENGTH
from app.models.base import Base


class Alarm(Base):
    __tablename__ = "alarm"
    __table_args__ = (
        Index("ux_alarm_open", "uuid", "kind", unique=True,
              postgresql_where=text("closed_at IS NULL")),
        Index("ix_alarm_closed_at", "closed_at"),
        Index("ix_alarm_uuid", "uuid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), nullable=False)
    #: LED_FAULT / BATT_LOW / … (core/alarm_rules.KINDS)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: warn(경고) / caution(주의) / info(정보)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    #: 조건이 처음 보인 시각(= 발생 시각).
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 연 시각. NULL = 아직 관찰 중(지속 시간이 안 참 — 화면에 안 보인다).
    opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 조건을 마지막으로 확인한 재조정 시각.
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 해제 시각. NULL = 열림(또는 관찰).
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 마지막 값(er·cv·md·횟수 등).
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
