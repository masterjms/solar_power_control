"""이력 — device_event (docs/03).

연결·오류·상태변경·응답을 한 테이블에 쌓는다. Telemetry 의 1/100 이하라 파티션은 없다.
uuid 에 FK 를 걸지 않는다 — 단말을 지워도 이력은 남긴다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Text, func
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import UUID_LENGTH
from app.models.base import Base


class DeviceEvent(Base):
    __tablename__ = "device_event"
    __table_args__ = (
        Index("ix_device_event_uuid_received_at", "uuid", "received_at"),
        # 알람 "재부팅 잦음"(오늘 REBOOT 횟수) — 0007
        Index("ix_device_event_kind_received_at", "kind", "received_at"),
        # QoS1 중복 배달 방어. NULL 은 비교 대상이 아니라 부분 인덱스로 건다.
        Index(
            "ux_device_event_dedup_key",
            "dedup_key",
            unique=True,
            postgresql_where="dedup_key IS NOT NULL",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), nullable=False)
    #: REGISTER / REGISTER_ACK / ONLINE / OFFLINE / LWT / REBOOT / LOST / ERR / STATE_CHANGE /
    #: CONFIG_SET / CONFIG_ACK / CMD_ACK / PONG (docs/03). 자유 텍스트 — CHECK 없음.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: `uuid:type:seq-or-cv:sha1(payload)[:16]`. 키를 못 만드는 종류는 NULL.
    dedup_key: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
