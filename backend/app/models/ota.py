"""OTA(원격 펌웨어 갱신, 문제점 56번, 사양서 §24) — ota_package · ota_job · ota_item (docs/03 "OTA").

  ota_package  올린 펌웨어. 서버가 `SolarLTE.bin` 을 base64 한 줄(pkg)로 바꿔 DB 에 둔다(최대 약 218KB) —
               따로 파일 볼륨이 없어 매일 DB 백업·설치형 묶음에 그대로 따라간다. sha256·size 는 pkg 기준.
  ota_job      한 번 보내기(단말 하나 / 법정동 하나 / 다시 보내기 목록). 파일 정보는 보낼 때 값으로 남긴다.
  ota_item     단말 하나. **id 가 OTA_START 의 job_id**(단말이 OTA_ACK 에 그대로 돌려준다, 32비트 부호 없음).
               다운로드 주소 /ota/<id>/<uuid>/<token> — token 은 항목마다 무작위 22자, 처음 보낸 때부터 2시간.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import UUID_LENGTH
from app.models.base import Base

#: 진행(waiting/sending/running)과 끝(나머지). 끝 코드는 단말 OTA_ACK result 그대로 + 서버 판단 3개.
OTA_OPEN = ("waiting", "sending", "running")
OTA_ITEM_STATUSES = (
    *OTA_OPEN,
    "DONE", "ROLLBACK", "BAD", "STATE", "BATT", "BUSY", "HTTP", "SIZE", "HASH", "FLASH",
    "NO_RESPONSE", "CANCELLED", "SKIPPED",
)


def _ts(nullable: bool = True):  # noqa: ANN202
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class OtaPackage(Base):
    __tablename__ = "ota_package"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 올린 파일 이름(예 SolarLTE.bin).
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    #: 올린 사람이 적은 버전(예 1.4.1). 단말 REGISTER fw 와 견줄 때 쓴다(지금은 참고, §24.5).
    version: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    #: 펌웨어(bin) 바이트 수 — 160KB 한도.
    fw_size: Mapped[int] = mapped_column(Integer, nullable=False)
    #: pkg(base64 한 줄) 글자 수 = OTA_START.size.
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    #: pkg SHA-256 64자리 소문자 hex = OTA_START.sha256. 단말이 위조를 막는 유일한 수단(§24.5).
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    #: pkg 본문(base64, ASCII).
    data: Mapped[str] = mapped_column(Text, nullable=False, deferred=True)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OtaJob(Base):
    __tablename__ = "ota_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ota_package.id", ondelete="SET NULL"), index=True
    )
    #: 보낼 때의 파일 정보(파일을 지워도 기록은 남는다).
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    #: device(단말 하나) / node(법정동 하나) / retry(앞 작업의 실패·응답 없음 단말)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text)
    scope_label: Mapped[str | None] = mapped_column(Text)
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 열린 항목이 없어진 시각.
    finished_at: Mapped[dt.datetime | None] = _ts()
    cancelled_at: Mapped[dt.datetime | None] = _ts()
    cancelled_by: Mapped[str | None] = mapped_column(Text)


class OtaItem(Base):
    __tablename__ = "ota_item"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in OTA_ITEM_STATUSES) + ")",
            name="ck_ota_item_status",
        ),
        UniqueConstraint("job_id", "uuid", name="uq_ota_item_job_uuid"),
        # 진행기(5초)가 훑는 것 — 열린 항목만.
        Index("ix_ota_item_open", "status", postgresql_where=text(
            "status IN ('waiting','sending','running')")),
        Index("ix_ota_item_uuid", "uuid"),
    )

    #: = OTA_START.job_id
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ota_job.id", ondelete="CASCADE"), nullable=False
    )
    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="waiting")
    #: 다운로드 주소의 마지막 칸(무작위 22자, base64url). 처음 보낼 때 정하고 다시 보내도 같다.
    token: Mapped[str | None] = mapped_column(Text)
    #: OTA_START 보낸 횟수(START 가 30초 안에 안 오면 같은 job_id 로 다시, 3회까지 — §24.4).
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    #: 처음 보낸 시각 = 주소 유효 2시간의 시작.
    first_sent_at: Mapped[dt.datetime | None] = _ts()
    sent_at: Mapped[dt.datetime | None] = _ts()
    #: OTA_ACK START 받은 시각 — 여기서 1시간 30분 안에 결과가 없으면 NO_RESPONSE.
    started_at: Mapped[dt.datetime | None] = _ts()
    finished_at: Mapped[dt.datetime | None] = _ts()
    #: 받은 다운로드 요청 수(조각 수 확인용).
    downloads: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: 보내기 전 단말 fw, 결과를 받은 때의 단말 fw(REGISTER).
    fw_before: Mapped[str | None] = mapped_column(Text)
    fw_after: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
