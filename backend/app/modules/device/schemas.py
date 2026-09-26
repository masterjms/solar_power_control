"""단말 API 요청/응답 스키마 (docs/05)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings
from app.constants import (
    KA_MAX_SEC,
    SITE_MAX_LEN,
    TI_MAX_SEC,
    DeviceState,
)


def _check_min(value: int | None, name: str, minimum: int) -> int | None:
    # 하한은 settings(개발 환경에서만 낮춤). pydantic Field(ge=) 는 import 시점 상수라 여기서 본다.
    if value is not None and value < minimum:
        raise ValueError(f"{name} 은 {minimum} 이상이어야 한다")
    return value


class DeviceOut(BaseModel):
    """`device` 컬럼 전부 + 계산 필드. 목록·상세 공용(last_telemetry 도 목록에 포함)."""

    model_config = ConfigDict(from_attributes=True)

    uuid: str
    state: str
    state_reason: str | None = None
    state_changed_at: dt.datetime | None = None
    register_ack_at: dt.datetime | None = None
    fw: str | None = None
    device_model: str | None = None
    modem_model: str | None = None
    imei: str | None = None
    iccid: str | None = None
    msisdn: str | None = None
    cv_device: int | None = None
    ss_device: int | None = None
    ti_device: int | None = None
    ka_device: int | None = None
    cv_server: int
    profile_id: int
    profile_name: str | None = None
    ti_override: int | None = None
    ka_override: int | None = None
    #: `override ?? profile` 적용값. CONFIG_SET 에 실리는 값.
    ti_effective: int
    ka_effective: int
    lat: float | None = None
    lon: float | None = None
    site: str | None = None
    address: str | None = None
    bjd_code: str | None = None
    grp: str | None = None
    last_register_at: dt.datetime | None = None
    last_telemetry_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None
    last_sq: int | None = None
    last_telemetry: dict[str, Any] | None = None
    #: 브로커 로그/LWT 플래그 (A). 화면은 is_online 을 쓴다.
    online: bool
    online_changed_at: dt.datetime | None = None
    offline_at: dt.datetime | None = None
    #: A AND C (core/presence.py).
    is_online: bool
    lost_count: int
    reboot_count: int
    config_sent_at: dt.datetime | None = None
    #: cv_server > 0 AND cv_device != cv_server. 0 이면 아직 보낸 적 없음 → false.
    config_pending: bool
    #: 단말 보고값(ti_device/ka_device) 이 서버 적용값과 다르다 (화면 1 "다르면 표시").
    config_mismatch: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class DevicePage(BaseModel):
    items: list[DeviceOut]
    total: int
    page: int
    size: int
    #: 필터와 무관한 전체 집계. 상태별 + online(is_online 기준).
    counts: dict[str, int]


_STATE_TARGETS = {s.value for s in DeviceState}


class StatePatch(BaseModel):
    state: str
    #: ACTIVE 로 갈 때 선택. 없으면 기존 값 유지. 24자 이내(단말 OLED).
    site: str | None = Field(default=None, max_length=SITE_MAX_LEN)
    #: REJECTED 사유 등. REJECTED 면 REGISTER_ACK reason 으로 나간다.
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("state")
    @classmethod
    def _upper_state(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in _STATE_TARGETS:
            raise ValueError(f"state 는 {sorted(_STATE_TARGETS)} 중 하나")
        return v


class StateOut(BaseModel):
    uuid: str
    state: str
    site: str | None
    #: 브로커 끊김이면 false — DB 는 커밋됐다. POST …/register-ack 로 재발행하거나 다음
    #: REGISTER 때 자동.
    published: bool
    register_ack: dict[str, Any]


class RegisterAckOut(BaseModel):
    uuid: str
    state: str
    published: bool
    #: RETIRED 는 빈 retain 만 보낸다 → cleared=true.
    cleared: bool
    register_ack: dict[str, Any] | None


class ConfigPatch(BaseModel):
    """전부 선택. 보낸 키만 바꾼다 — `ti_override: null` 은 override 해제(프로필 값으로)."""

    profile_id: int | None = None
    ti_override: int | None = Field(default=None, ge=1, le=TI_MAX_SEC)
    ka_override: int | None = Field(default=None, ge=1, le=KA_MAX_SEC)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    site: str | None = Field(default=None, max_length=SITE_MAX_LEN)
    address: str | None = Field(default=None, max_length=500)
    bjd_code: str | None = Field(default=None, min_length=10, max_length=10)

    @field_validator("ti_override")
    @classmethod
    def _ti_min(cls, v: int | None) -> int | None:
        return _check_min(v, "ti_override", settings.config_ti_min_sec)

    @field_validator("ka_override")
    @classmethod
    def _ka_min(cls, v: int | None) -> int | None:
        return _check_min(v, "ka_override", settings.config_ka_min_sec)


class ConfigPatchOut(BaseModel):
    uuid: str
    state: str
    cv_server: int
    profile_id: int
    ti_override: int | None
    ka_override: int | None
    ti_effective: int
    ka_effective: int
    lat: float | None
    lon: float | None
    site: str | None
    address: str | None
    bjd_code: str | None
    #: 적용값이 바뀌어 cv_server 를 올렸다.
    cv_bumped: bool
    #: CONFIG_SET 을 즉시 발행했다(ACTIVE 만). false 면 reason 을 본다.
    published: bool
    #: NOT_ACTIVE | NOT_NEEDED | PUBLISH_FAILED | null
    reason: str | None
    #: 발행했거나(published) 다음 송신 때 나갈 CONFIG_SET 전체값.
    payload: dict[str, Any] | None
    #: site 가 바뀌어 REGISTER_ACK 를 다시 retain 했다.
    register_ack_republished: bool


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


class DeleteOut(BaseModel):
    uuid: str
    deleted: bool
    #: REGISTER_ACK retain 을 지웠다. 브로커 끊김이면 false (행은 지워졌다).
    retain_cleared: bool
