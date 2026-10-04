"""스케줄 배포(S-25) — schedule_profile · schedule_assign · deploy_job · deploy_item · device_schedule
(docs/03 "스케줄 배포", ADR-010).

메시지는 새로 없다 — S-23 `SETTINGS_SET` 에 `tbl`(조건 + crc)을 실어 단말마다 보낸다(사양서 §13.1).
프로필 = 표 조건(region·lat_e6·lon_e6·on·off, crc) + 운전 15개(스케줄 3 + 다단계 12).
단말별 10개(기준 밝기 3·fade·배터리 6)는 프로필에 없다 — 배포 때 그 단말의 마지막 읽은 값을 그대로 보낸다.
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
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import UUID_LENGTH
from app.models.base import Base

#: deploy_item.status — 진행(waiting/reading/sent)과 끝(나머지).
DEPLOY_ITEM_STATUSES = (
    "waiting", "reading", "sent",
    "OK", "CRC", "RULE", "RANGE", "BAD", "STATE", "FLASH", "NO_RESPONSE", "READ_FAILED",
    "CANCELLED", "SUPERSEDED",
)


def _ts(nullable: bool = True) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class ScheduleProfile(Base):
    __tablename__ = "schedule_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: 저장할 때마다 +1. 배포 기록(deploy_job·device_schedule)이 어느 판인지 남긴다.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    #: 표 조건 — 단말 `tbl` 과 같은 단위(좌표 x1e6, 보정 분).
    region: Mapped[str] = mapped_column(Text, nullable=False)
    lat_e6: Mapped[int] = mapped_column(Integer, nullable=False)
    lon_e6: Mapped[int] = mapped_column(Integer, nullable=False)
    on_corr: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    off_corr: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    #: suntable.table_crc32(build_table(...)) — 8자리 대문자 hex. 조건이 바뀌면 다시 계산.
    crc: Mapped[str] = mapped_column(CHAR(8), nullable=False)
    #: 운전 15개 {key: 단말 정수}. 키 목록 = core/schedule_rules.PROFILE_KEYS.
    values: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    #: 주소 검색으로 고른 원래 주소(참고).
    address: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(Text)
    #: 만든 사람 — 지역관리자는 자기가 만든 양식만 고치고 지운다(문제점 21번). 옛 행은 NULL(최고관리자만).
    created_by: Mapped[str | None] = mapped_column(Text)


class ScheduleAssign(Base):
    """법정동 트리 노드 또는 단말 하나 → 프로필. 단말 배정이 노드보다 우선, 노드는 가장 가까운 조상."""

    __tablename__ = "schedule_assign"
    __table_args__ = (
        CheckConstraint("(node_id IS NULL) <> (uuid IS NULL)", name="ck_schedule_assign_target"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("region.id", ondelete="CASCADE"), unique=True
    )
    uuid: Mapped[str | None] = mapped_column(
        CHAR(UUID_LENGTH), ForeignKey("device.uuid", ondelete="CASCADE"), unique=True
    )
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("schedule_profile.id", ondelete="RESTRICT"), nullable=False,
        index=True,
    )
    assigned_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    assigned_by: Mapped[str | None] = mapped_column(Text)


class DeployJob(Base):
    __tablename__ = "deploy_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("schedule_profile.id", ondelete="SET NULL"), index=True
    )
    profile_name: Mapped[str] = mapped_column(Text, nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    crc: Mapped[str] = mapped_column(CHAR(8), nullable=False)
    #: 보낼 body 의 프로필 부분(조건 + 15개) — 배포 도중 프로필이 바뀌어도 이 판으로 끝까지 보낸다.
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: profile(그 프로필이 걸린 단말 전부) / node(노드 아래) / device(단말 하나)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text)
    scope_label: Mapped[str | None] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 대기·진행 항목이 없어진 시각(오프라인 대기가 남으면 NULL).
    finished_at: Mapped[dt.datetime | None] = _ts()
    cancelled_at: Mapped[dt.datetime | None] = _ts()


class DeployItem(Base):
    __tablename__ = "deploy_item"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in DEPLOY_ITEM_STATUSES) + ")",
            name="ck_deploy_item_status",
        ),
        Index("ix_deploy_item_open", "uuid",
              postgresql_where=text("status IN ('waiting','reading','sent')")),
    )

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("deploy_job.id", ondelete="CASCADE"), primary_key=True
    )
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="waiting")
    #: 이 항목이 낸 SETTINGS 요청의 첫 seq(재발송은 새 seq — command 행 created_by 로 이어 찾는다).
    first_seq: Mapped[int | None] = mapped_column(BigInteger)
    #: 이 항목으로 SETTINGS_SET 을 시작한 횟수("응답 없는 단말만 다시" 포함). 한 번에 3회 재발송은 settings_sync.
    rounds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sent_at: Mapped[dt.datetime | None] = _ts()
    acked_at: Mapped[dt.datetime | None] = _ts()
    #: 단말 결과 원문 또는 서버 판단(READ_FAILED 등) 설명.
    detail: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DeviceSchedule(Base):
    """단말에 **적용된**(ACK OK) 스케줄 — 스케줄 불일치 알람과 "적용됨 M대" 의 기준."""

    __tablename__ = "device_schedule"

    uuid: Mapped[str] = mapped_column(
        CHAR(UUID_LENGTH), ForeignKey("device.uuid", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("schedule_profile.id", ondelete="SET NULL")
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    crc: Mapped[str] = mapped_column(CHAR(8), nullable=False)
    #: 적용한 15개(프로필 판 그대로).
    values: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    job_id: Mapped[int | None] = mapped_column(Integer)
    applied_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
