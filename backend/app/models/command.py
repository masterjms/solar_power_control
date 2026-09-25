"""서버 → 단말 명령 — command · command_ack (docs/03).

seq 는 DB 시퀀스 `cmd_seq`(app/core/ids.py). 메모리 카운터 금지 — 재시작 때 되감기면
단말이 이미 본 seq 가 다시 나와 중복 실행 방지가 깨진다.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import SEQ_MAX, UUID_LENGTH
from app.models.base import Base


class Command(Base):
    __tablename__ = "command"
    __table_args__ = (
        CheckConstraint(f"seq BETWEEN 0 AND {SEQ_MAX}", name="ck_command_seq_uint32"),
        CheckConstraint(
            "target_kind IN ('device','group','all')", name="ck_command_target_kind"
        ),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    #: device / group / all
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: uuid 또는 group_id. all 은 NULL.
    target_id: Mapped[str | None] = mapped_column(Text, index=True)
    #: PING / CMD / SCH / OTA / STATUS_GET …
    type: Mapped[str] = mapped_column(Text, nullable=False)
    #: 보낸 그대로.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: 대상 단말 수(그룹/전체). 개별은 1.
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    acked_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sent_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: OK / PARTIAL / TIMEOUT / FAILED
    result: Mapped[str | None] = mapped_column(Text)


class CommandAck(Base):
    """단말별 응답. 그룹 명령(5차)에서 누가 답했는지 집계하는 근거."""

    __tablename__ = "command_ack"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), primary_key=True)
    result: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
