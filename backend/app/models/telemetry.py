"""이력 — telemetry (월 파티션) · telemetry_daily (일 집계). docs/03.

telemetry 는 `PARTITION BY RANGE (received_at)` 이라 ORM 으로 create_all 하지 않는다 —
파티션 생성은 alembic 0001 과 app/tasks/partitions.py 가 한다. 이 모델은 INSERT/SELECT
문을 만들 때만 쓴다.

숫자 컬럼을 풀어 두는 이유: 그래프·집계 쿼리에서 jsonb 파싱을 피하려고. 단위 변환(÷100)은
하지 않는다. `cs` 비트 해석도 저장하지 않는다(MPPT 레지스터 맵 변경 대비).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, Float, Integer, SmallInteger, Text
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import UUID_LENGTH
from app.models.base import Base


class Telemetry(Base):
    __tablename__ = "telemetry"
    __table_args__ = {"postgresql_partition_by": "RANGE (received_at)"}

    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    #: 단말 RTC 'YYMMDDThhmm' 원문 (KST, 오프셋 없음). 참고값.
    ts_device: Mapped[str | None] = mapped_column(Text)
    sq: Mapped[int | None] = mapped_column(BigInteger)
    fw: Mapped[str | None] = mapped_column(Text)
    ss: Mapped[int | None] = mapped_column(Integer)
    cv: Mapped[int | None] = mapped_column(Integer)
    er: Mapped[int | None] = mapped_column(Integer)
    #: `on` 은 SQL 예약어라 파이썬 속성명은 on_ 으로 둔다.
    on_: Mapped[int | None] = mapped_column("on", SmallInteger)
    md: Mapped[int | None] = mapped_column(SmallInteger)
    pw1: Mapped[int | None] = mapped_column(SmallInteger)
    pw2: Mapped[int | None] = mapped_column(SmallInteger)
    pw3: Mapped[int | None] = mapped_column(SmallInteger)
    bv: Mapped[int | None] = mapped_column(Integer)
    bi: Mapped[int | None] = mapped_column(Integer)
    sc: Mapped[int | None] = mapped_column(SmallInteger)
    pp: Mapped[int | None] = mapped_column(Integer)
    li: Mapped[int | None] = mapped_column(Integer)
    cs: Mapped[int | None] = mapped_column(Integer)
    #: 원본 전체. 해석은 조회 시.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class TelemetryDaily(Base):
    __tablename__ = "telemetry_daily"

    uuid: Mapped[str] = mapped_column(CHAR(UUID_LENGTH), primary_key=True)
    #: KST 날짜.
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    samples: Mapped[int] = mapped_column(Integer, nullable=False)
    #: PV 발전량 추정(Wh). pp/100 의 시간 적분.
    pp_wh: Mapped[float | None] = mapped_column(Float)
    #: 부하 소비(Ah). li/100 의 시간 적분.
    li_ah: Mapped[float | None] = mapped_column(Float)
    bv_min: Mapped[int | None] = mapped_column(Integer)
    bv_max: Mapped[int | None] = mapped_column(Integer)
    sc_min: Mapped[int | None] = mapped_column(SmallInteger)
    sc_max: Mapped[int | None] = mapped_column(SmallInteger)
    #: 점등 시간 추정(분).
    on_minutes: Mapped[int | None] = mapped_column(Integer)
