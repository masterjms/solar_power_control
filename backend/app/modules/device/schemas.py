"""단말 API 요청/응답 스키마."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.constants import TI_MAX_SEC, TI_MIN_SEC


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uuid: str
    state: str
    state_reason: str | None = None
    has_mqtt_account: bool
    fw: str | None = None
    device_model: str | None = None
    modem_model: str | None = None
    imei: str | None = None
    iccid: str | None = None
    msisdn: str | None = None
    cv_device: int | None = None
    ss_device: int | None = None
    ti_device: int | None = None
    cv_server: int
    ti_server: int
    lat: float | None = None
    lon: float | None = None
    site: str | None = None
    grp0: str | None = None
    grp1: str | None = None
    last_register_at: dt.datetime | None = None
    last_telemetry_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None
    last_sq: int | None = None
    online: bool
    #: presence 규칙으로 계산한 값. `online` 컬럼(LWT, 3차)과 구분한다.
    is_online: bool
    offline_at: dt.datetime | None = None
    lost_count: int
    reboot_count: int
    config_sent_at: dt.datetime | None = None
    config_pending: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class DeviceDetailOut(DeviceOut):
    last_telemetry: dict[str, Any] | None = None


class DevicePage(BaseModel):
    items: list[DeviceOut]
    total: int
    page: int
    size: int


class ConfigPatch(BaseModel):
    ti: int | None = Field(default=None, ge=TI_MIN_SEC, le=TI_MAX_SEC)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    site: str | None = Field(default=None, max_length=100)


class ConfigPatchOut(BaseModel):
    uuid: str
    cv_server: int
    ti_server: int
    lat: float | None
    lon: float | None
    site: str | None
    published: bool
    payload: dict[str, Any]


class PingOut(BaseModel):
    uuid: str
    seq: int


class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    received_at: dt.datetime
    ts_device: str | None
    sq: int | None
    fw: str | None
    ss: int | None
    cv: int | None
    er: int | None
    on: int | None
    md: int | None
    pw: list[int | None]
    bv: int | None
    bi: int | None
    sc: int | None
    pp: int | None
    li: int | None
    cs: int | None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    payload: dict[str, Any] | None
    received_at: dt.datetime


class ImportAccountsOut(BaseModel):
    imported: int
    created: int
    updated: int
    errors: list[str]
    export_enabled: bool
    passwd_md5: str | None
    acl_md5: str | None
    acl_applied: bool


class DeleteOut(BaseModel):
    uuid: str
    deleted: bool
    export_enabled: bool
    acl_applied: bool
