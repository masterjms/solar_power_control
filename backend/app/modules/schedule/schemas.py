from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class ProfileIn(BaseModel):
    """스케줄 프로필 만들기/고치기. 좌표는 실수(도), 보정은 분. values = 운전 15개(단말 정수)."""

    name: str = Field(min_length=1, max_length=60)
    region: str
    lat: float
    lon: float
    on: int = 0
    off: int = 0
    values: dict[str, int]
    address: str | None = None


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    region: str | None = None
    lat: float | None = None
    lon: float | None = None
    on: int | None = None
    off: int | None = None
    values: dict[str, int] | None = None
    address: str | None = None


class ProfileOut(BaseModel):
    id: int
    name: str
    version: int
    region: str
    lat_e6: int
    lon_e6: int
    on: int
    off: int
    crc: str
    values: dict[str, int]
    address: str | None
    updated_at: dt.datetime
    updated_by: str | None
    #: 이 프로필이 걸린 노드·단말 배정 수
    assigned_nodes: int = 0
    assigned_devices: int = 0
    #: 이 프로필이 적용 대상인 운영(ACTIVE) 단말 수(노드 상속 포함) / 그중 이 판이 적용된 수
    targets: int = 0
    applied: int = 0


class AssignIn(BaseModel):
    """node_id 또는 uuid 하나. profile_id null = 배정 해제."""

    node_id: int | None = None
    uuid: str | None = None
    profile_id: int | None = None


class AssignOut(BaseModel):
    id: int
    node_id: int | None
    uuid: str | None
    profile_id: int
    profile_name: str
    label: str | None
    assigned_at: dt.datetime
    assigned_by: str | None


class DeviceScheduleOut(BaseModel):
    """단말 하나의 스케줄 상태(목록 행·단말 상세 스케줄 탭)."""

    uuid: str
    site: str | None
    state: str
    is_online: bool
    node_path: str | None
    #: 배정(상속 포함) 프로필
    profile_id: int | None
    profile_name: str | None
    profile_version: int | None
    profile_crc: str | None
    #: device / node:<id> / None
    source: str | None
    #: 적용된 판(ACK OK)
    applied_profile_id: int | None = None
    applied_version: int | None = None
    applied_crc: str | None = None
    applied_at: dt.datetime | None = None
    #: 단말이 마지막으로 알려 준 표(SETTINGS·ACK)
    device_crc: str | None = None
    device_region: str | None = None
    device_src: int | None = None
    #: 배정 프로필 이 판이 적용됐고 단말 표 crc 도 같다
    applied_ok: bool = False
    #: 진행 중인 배포 항목 상태(waiting/reading/sent) · 작업 id
    deploy_status: str | None = None
    deploy_job_id: int | None = None
    #: DIP4(다단계) — SETTINGS dev.dip bit3. None = 모름
    dip4: bool | None = None
    #: 오늘 점등·소등(배정 프로필 조건으로 서버 계산, "HH:MM")
    today_on: str | None = None
    today_off: str | None = None


class DevicesPage(BaseModel):
    items: list[DeviceScheduleOut]
    total: int
    page: int
    size: int


class DeployIn(BaseModel):
    profile_id: int
    #: profile = 그 프로필이 걸린 단말 전부 / node = 그 노드 아래 / device = 단말 하나
    scope: str = Field(pattern="^(profile|node|device)$")
    scope_id: str | None = None


class DeployItemOut(BaseModel):
    uuid: str
    site: str | None
    is_online: bool
    status: str
    rounds: int
    sent_at: dt.datetime | None
    acked_at: dt.datetime | None
    detail: str | None


class DeployJobOut(BaseModel):
    id: int
    profile_id: int | None
    profile_name: str
    profile_version: int
    crc: str
    scope_kind: str
    scope_id: str | None
    scope_label: str | None
    total: int
    created_by: str | None
    created_at: dt.datetime
    finished_at: dt.datetime | None
    cancelled_at: dt.datetime | None
    #: 상태별 수 {waiting, reading, sent, OK, …}
    counts: dict[str, int]
    items: list[DeployItemOut] | None = None


class RetryIn(BaseModel):
    #: 비우면 다시 보낼 수 있는 항목 전부(응답 없음·실패).
    uuids: list[str] | None = None


class RetryOut(BaseModel):
    retried: int
    skipped: int
