"""단말 — device (docs/03 §device).

최신값 캐시(last_telemetry 등)와 서버 의도값(cv_server, ti_server, lat, lon)을 한 행에
둔다. 이력은 telemetry / device_event 로 간다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import TI_MAX_SEC, TI_MIN_SEC, UUID_LENGTH, DeviceState
from app.models.base import Base


class Device(Base):
    __tablename__ = "device"
    __table_args__ = (
        CheckConstraint("uuid ~ '^[0-9A-F]{24}$'", name="ck_device_uuid_format"),
        CheckConstraint(
            f"ti_server BETWEEN {TI_MIN_SEC} AND {TI_MAX_SEC}", name="ck_device_ti_server_range"
        ),
        CheckConstraint(
            "state IN ('PENDING','ACTIVE','SUSPENDED','REJECTED','RETIRED')",
            name="ck_device_state",
        ),
    )

    #: 24자리 대문자 16진수. topic 의 UUID 가 그대로 PK 다.
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), primary_key=True)

    #: 2차는 ACTIVE 고정(사양서 §2). 3차부터 기본 PENDING 으로 바꾼다.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DeviceState.ACTIVE.value, index=True
    )
    state_reason: Mapped[str | None] = mapped_column(Text)
    state_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    #: mosquitto `$7$101$…` PBKDF2. NULL 이면 계정 미발급. 평문은 저장하지 않는다.
    mqtt_password_hash: Mapped[str | None] = mapped_column(Text)

    # ── REGISTER 보강 정보 ──────────────────────────────
    fw: Mapped[str | None] = mapped_column(Text)
    device_model: Mapped[str | None] = mapped_column(Text)
    modem_model: Mapped[str | None] = mapped_column(Text)
    imei: Mapped[str | None] = mapped_column(Text)
    iccid: Mapped[str | None] = mapped_column(Text)
    msisdn: Mapped[str | None] = mapped_column(Text)

    # ── 단말 보고값 (REGISTER / Telemetry) ──────────────
    cv_device: Mapped[int | None] = mapped_column(Integer)
    ss_device: Mapped[int | None] = mapped_column(Integer)
    ti_device: Mapped[int | None] = mapped_column(Integer)

    # ── 서버 의도값 ─────────────────────────────────────
    #: `cv_server != cv_device` 면 다음 Telemetry 수신 시점에 CONFIG_SET 재전송(ADR-002).
    cv_server: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ti_server: Mapped[int] = mapped_column(Integer, nullable=False, server_default="600")
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    #: 장소명. 단말에 안 내려간다(사양서 §1.1.7).
    site: Mapped[str | None] = mapped_column(Text)
    #: 5차 그룹. 숫자 8~16자리.
    grp0: Mapped[str | None] = mapped_column(Text, index=True)
    grp1: Mapped[str | None] = mapped_column(Text, index=True)

    # ── 최신값 캐시 ─────────────────────────────────────
    last_register_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_telemetry_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    #: 어떤 메시지든 마지막 수신. LWT 는 브로커가 대신 보내는 것이라 갱신하지 않는다.
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_sq: Mapped[int | None] = mapped_column(BigInteger)
    #: 마지막 TM payload 원본.
    last_telemetry: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: LWT/CONNECT 로 갱신(3차). 2차는 presence 가 ti×factor 로 계산한다.
    online: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    offline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    lost_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reboot_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: 마지막 CONFIG_SET 발행 시각.
    config_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
