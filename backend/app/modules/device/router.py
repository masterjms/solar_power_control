"""/api/devices — docs/05_API.md"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.modules.deps import get_config_sync, get_publisher
from app.modules.device import service
from app.modules.device.schemas import (
    ConfigPatch,
    ConfigPatchOut,
    DeleteOut,
    DeviceOut,
    DevicePage,
    EventOut,
    PingOut,
    RegisterAckOut,
    StateOut,
    StatePatch,
    TelemetryOut,
)
from app.mqtt.config_sync import ConfigSyncQueue
from app.mqtt.publisher import MqttPublisher

router = APIRouter(prefix="/api/devices", tags=["devices"])

_UUID_PATH = "{uuid}"


def _uuid(uuid: str) -> str:
    """경로의 uuid 를 대문자로 접는다. 소문자로 쳐도 같은 단말이다."""
    return uuid.strip().upper()


@router.get("", response_model=DevicePage)
async def list_devices(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    state: str | None = Query(default=None),
    online: bool | None = Query(default=None, description="is_online 판정값으로 필터"),
    q: str | None = Query(default=None, max_length=64, description="uuid 또는 site 부분 일치"),
    db: AsyncSession = Depends(get_db),
) -> DevicePage:
    return await service.list_devices(db, page=page, size=size, state=state, online=online, q=q)


@router.get(f"/{_UUID_PATH}", response_model=DeviceOut)
async def get_device(uuid: str, db: AsyncSession = Depends(get_db)) -> DeviceOut:
    return await service.get_device(db, _uuid(uuid))


@router.get(f"/{_UUID_PATH}/telemetry", response_model=list[TelemetryOut])
async def get_telemetry(
    uuid: str,
    since: dt.datetime | None = Query(default=None, alias="from"),
    until: dt.datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> list[TelemetryOut]:
    return await service.list_telemetry(db, _uuid(uuid), since=since, until=until, limit=limit)


@router.get(f"/{_UUID_PATH}/events", response_model=list[EventOut])
async def get_events(
    uuid: str,
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[EventOut]:
    return await service.list_events(db, _uuid(uuid), kind=kind, limit=limit)


@router.patch(f"/{_UUID_PATH}/state", response_model=StateOut)
async def patch_state(
    uuid: str,
    body: StatePatch,
    db: AsyncSession = Depends(get_db),
    publisher: MqttPublisher = Depends(get_publisher),
) -> StateOut:
    return await service.set_state(db, _uuid(uuid), body, publisher)


@router.post(f"/{_UUID_PATH}/register-ack", response_model=RegisterAckOut)
async def republish_register_ack(
    uuid: str,
    db: AsyncSession = Depends(get_db),
    publisher: MqttPublisher = Depends(get_publisher),
) -> RegisterAckOut:
    return await service.republish_register_ack(db, _uuid(uuid), publisher)


@router.patch(f"/{_UUID_PATH}/config", response_model=ConfigPatchOut)
async def patch_config(
    uuid: str,
    body: ConfigPatch,
    db: AsyncSession = Depends(get_db),
    publisher: MqttPublisher = Depends(get_publisher),
    config_sync: ConfigSyncQueue = Depends(get_config_sync),
) -> ConfigPatchOut:
    return await service.patch_config(db, _uuid(uuid), body, publisher, config_sync)


@router.post(f"/{_UUID_PATH}/ping", response_model=PingOut)
async def ping(
    uuid: str,
    db: AsyncSession = Depends(get_db),
    publisher: MqttPublisher = Depends(get_publisher),
) -> PingOut:
    return await service.ping(db, _uuid(uuid), publisher)


@router.delete(f"/{_UUID_PATH}", response_model=DeleteOut)
async def delete_device(
    uuid: str,
    db: AsyncSession = Depends(get_db),
    publisher: MqttPublisher = Depends(get_publisher),
) -> DeleteOut:
    return await service.delete_device(db, _uuid(uuid), publisher)
