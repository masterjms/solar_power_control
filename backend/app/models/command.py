"""서버 → 단말 명령 — command · command_ack · command_target (docs/03).

seq 는 DB 시퀀스 `cmd_seq`(app/core/ids.py). 메모리 카운터 금지 — 재시작 때 되감기면
단말이 이미 본 seq 가 다시 나와 중복 실행 방지가 깨진다.
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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import SEQ_MAX, UUID_LENGTH
from app.models.base import Base


class Command(Base):
    __tablename__ = "command"
    __table_args__ = (
        CheckConstraint(f"seq BETWEEN 0 AND {SEQ_MAX}", name="ck_command_seq_uint32"),
        CheckConstraint(
            "target_kind IN ('device','node','all')", name="ck_command_target_kind"
        ),
        # 종료 판정 타이머(30초)가 도는 대상. 끝난 명령은 인덱스에서 빠진다.
        Index("ix_command_unfinished", "seq", postgresql_where="finished_at IS NULL"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    #: device / node / all (5차 — 2차의 group 은 트리 노드 node 로 바뀌었다)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: uuid 또는 region.id 문자열. all 은 NULL.
    target_id: Mapped[str | None] = mapped_column(Text, index=True)
    #: PING / COMMAND (6차 SCH, 7차 OTA)
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
    #: 5차. 명령을 낸 관리자(X-Remote-User). PING 등 옛 행은 NULL.
    created_by: Mapped[str | None] = mapped_column(Text)
    #: 5차. 실제로 발행한 topic 목록(상위 노드면 하위 말단 수만큼).
    topics: Mapped[list[str] | None] = mapped_column(JSONB)
    #: 5차. payload.exp 복사본(조회용).
    exp: Mapped[int | None] = mapped_column(Integer)


class CommandTarget(Base):
    """COMMAND 대상 스냅숏 + 단말별 상태 (ADR-005).

    보낼 때의 ACTIVE 단말을 박아 둔다 — 나중에 트리에서 빠지거나 승인이 풀려도 "그때 누구에게
    보냈나"가 남아야 응답 집계와 개별 재시도를 할 수 있다.
    """

    __tablename__ = "command_target"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','OK','LOCAL','EXPIRED','BAD','STATE')",
            name="ck_command_target_status",
        ),
        # 단말 메시지 수신 직후 "이 단말에 다시 보낼 명령이 있나" 조회용(자동 재시도).
        Index("ix_command_target_uuid_status", "uuid", "status"),
    )

    seq: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("command.seq", ondelete="CASCADE"), primary_key=True
    )
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), primary_key=True)
    #: pending / OK / LOCAL / EXPIRED / BAD / STATE
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    #: 발송 횟수(첫 발송 포함).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    last_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: 마지막 COMMAND_ACK 원본.
    ack: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


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
