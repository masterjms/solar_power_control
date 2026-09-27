"""단말 설정 API 스키마 (docs/05 "단말 설정 API", ADR-007).

값 검사(범위·규칙·region)는 core/settings_rules 가 한다 — 여기서 pydantic 으로 막으면 에러 코드가
VALIDATION_FAILED 로 뭉개진다(docs/05 는 SETTINGS_RANGE/RULE/INCOMPLETE 로 나눈다).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel


class TblIn(BaseModel):
    region: Any = None
    lat: Any = None
    lon: Any = None
    on: Any = 0
    off: Any = 0


class SettingsPut(BaseModel):
    values: dict[str, Any] | None = None
    tbl: TblIn | None = None


class TblOut(BaseModel):
    region: str | None = None
    lat_e6: int | None = None
    lon_e6: int | None = None
    on: int | None = None
    off: int | None = None
    src: int | None = None
    ss: int | None = None
    crc: str | None = None
    #: 같은 조건으로 서버가 계산한 CRC. 다르면 "현장에서 손댄 표".
    crc_expected: str | None = None
    matches: bool | None = None


class DevOut(BaseModel):
    dip: int | None = None
    bat: int | None = None


class PendingOut(BaseModel):
    kind: str
    seq: int
    sent_at: dt.datetime | None = None
    attempts: int | None = None


class DiffRow(BaseModel):
    key: str
    db: int | None = None
    device: int | None = None


class SettingsOut(BaseModel):
    uuid: str
    sync: str
    read_at: dt.datetime | None = None
    values: dict[str, int] | None = None
    sh_db: str | None = None
    sh_device: str | None = None
    tbl: TblOut | None = None
    dev: DevOut | None = None
    ss_known: int | None = None
    #: device.ss_device (마지막 Telemetry/REGISTER 의 ss).
    ss_telemetry: int | None = None
    report: dict[str, int] | None = None
    diff: list[DiffRow] = []
    pending: PendingOut | None = None
    last_result: str | None = None
    last_result_at: dt.datetime | None = None


class SentOut(BaseModel):
    seq: int
    sent_at: dt.datetime


class WriteOut(SentOut):
    sh_expected: str
    payload_bytes: int


class HistoryOut(BaseModel):
    changed_at: dt.datetime
    by: str
    key: str
    old: int | None = None
    new: int | None = None
    note: str | None = None


class PreviewRow(BaseModel):
    month: int
    day: int
    on: str
    off: str
    hours: float


class SchedulePreviewOut(BaseModel):
    crc: str
    lat_e6: int
    lon_e6: int
    on: int
    off: int
    rows: list[PreviewRow]
