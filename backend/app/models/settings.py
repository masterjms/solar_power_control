"""단말 운전 설정(S-23) — device_settings · device_settings_history (docs/03 "단말 설정", ADR-007).

25개 항목 컬럼 이름 = ui_items.json key. 값은 **단말 정수 그대로**(전압 x100). NULL = 읽지 않음.
tests/test_settings_rules.py 가 컬럼 목록과 ui_items.json 키가 같은지 확인한다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import UUID_LENGTH
from app.models.base import Base


def _v() -> Mapped[int | None]:
    return mapped_column(Integer)


class DeviceSettings(Base):
    __tablename__ = "device_settings"
    __table_args__ = (
        CheckConstraint(
            "sync IN ('unknown','synced','writing','local_saved','device_changed')",
            name="ck_device_settings_sync",
        ),
        CheckConstraint(
            "pending_kind IS NULL OR pending_kind IN ('SETTINGS_GET','SETTINGS_SET')",
            name="ck_device_settings_pending_kind",
        ),
    )

    uuid: Mapped[str] = mapped_column(
        CHAR(UUID_LENGTH), ForeignKey("device.uuid", ondelete="CASCADE"), primary_key=True
    )

    # ── 25개 (ui_items.json 순서) ──────────────────────────
    start_ofst: Mapped[int | None] = _v()
    stop_ofst: Mapped[int | None] = _v()
    start_pwm: Mapped[int | None] = _v()
    manual_40w: Mapped[int | None] = _v()
    manual_5w1: Mapped[int | None] = _v()
    manual_5w2: Mapped[int | None] = _v()
    fade: Mapped[int | None] = _v()
    stage1_h: Mapped[int | None] = _v()
    stage1_m: Mapped[int | None] = _v()
    stage1_pwm: Mapped[int | None] = _v()
    stage2_h: Mapped[int | None] = _v()
    stage2_m: Mapped[int | None] = _v()
    stage2_pwm: Mapped[int | None] = _v()
    stage3_h: Mapped[int | None] = _v()
    stage3_m: Mapped[int | None] = _v()
    stage3_pwm: Mapped[int | None] = _v()
    stage4_h: Mapped[int | None] = _v()
    stage4_m: Mapped[int | None] = _v()
    stage4_pwm: Mapped[int | None] = _v()
    cut12: Mapped[int | None] = _v()
    rtn12: Mapped[int | None] = _v()
    cut24: Mapped[int | None] = _v()
    rtn24: Mapped[int | None] = _v()
    cut_time: Mapped[int | None] = _v()
    rtn_time: Mapped[int | None] = _v()

    # ── 스케줄 조건(단말 보고값 또는 서버가 쓴 값) ──────────
    tbl_region: Mapped[str | None] = mapped_column(Text)
    tbl_lat_e6: Mapped[int | None] = mapped_column(Integer)
    tbl_lon_e6: Mapped[int | None] = mapped_column(Integer)
    tbl_on: Mapped[int | None] = mapped_column(Integer)
    tbl_off: Mapped[int | None] = mapped_column(Integer)
    #: 0 펌웨어 기본 표 / 1 PC 도구 / 2 서버.
    tbl_src: Mapped[int | None] = mapped_column(SmallInteger)
    tbl_crc: Mapped[str | None] = mapped_column(CHAR(8))

    # ── 현장 스위치(읽기 전용) ────────────────────────────
    dip: Mapped[int | None] = mapped_column(SmallInteger)
    bat: Mapped[int | None] = mapped_column(SmallInteger)

    #: 마지막 SETTINGS / SETTINGS_ACK 의 sh.
    sh_device: Mapped[str | None] = mapped_column(CHAR(8))
    #: 서버가 확인한 스케줄 저장 번호(ACK ss, SETTINGS tbl.ss). Telemetry ss 와 다르면 local_saved.
    ss_known: Mapped[int | None] = mapped_column(Integer)
    #: 마지막 전체 읽기. NULL = 읽지 않음.
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    sync: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    #: 마지막 SETTINGS 원본(device_changed 일 때 받아들이기 재료).
    last_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # ── 응답 대기 중인 GET/SET (재발송용) ─────────────────
    pending_seq: Mapped[int | None] = mapped_column(BigInteger)
    pending_kind: Mapped[str | None] = mapped_column(Text)
    #: {"body": SET 몸통(v, tbl?) | null, "prev_sync": 보내기 전 sync, "by": 관리자}
    pending_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pending_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    pending_attempts: Mapped[int | None] = mapped_column(Integer)

    #: OK / RANGE / RULE / CRC / BAD / STATE / FLASH / TIMEOUT
    last_result: Mapped[str | None] = mapped_column(Text)
    last_result_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DeviceSettingsHistory(Base):
    """바뀐 key 한 개 = 1행. 표 조건은 key `tbl` + note(JSON 요약)."""

    __tablename__ = "device_settings_history"
    __table_args__ = (
        Index("ix_device_settings_history_uuid_changed_at", "uuid", "changed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), nullable=False)
    changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: device_read / server_write / 관리자 사용자명(X-Remote-User)
    by: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    old: Mapped[int | None] = mapped_column(Integer)
    new: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
