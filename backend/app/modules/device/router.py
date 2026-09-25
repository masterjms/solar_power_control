"""/api/devices — docs/05_API.md"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.errors import ValidationFailed
from app.modules.deps import get_config_sync, get_publisher
from app.modules.device import service
from app.modules.device.schemas import (
    ConfigPatch,
    ConfigPatchOut,
    DeleteOut,
    DeviceDetailOut,
    DevicePage,
    EventOut,
    ImportAccountsOut,
    PingOut,
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
    online: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> DevicePage:
    return await service.list_devices(db, page=page, size=size, state=state, online=online)


# import-accounts 는 /{uuid} 보다 먼저 선언해야 경로가 uuid 로 잡히지 않는다.
@router.post("/import-accounts", response_model=ImportAccountsOut)
async def import_accounts(
    file: UploadFile = File(..., description="uuid,password CSV"),
    db: AsyncSession = Depends(get_db),
) -> ImportAccountsOut:
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("CSV 는 UTF-8 이어야 합니다.") from exc
    return await service.import_accounts(db, content)


@router.get(f"/{_UUID_PATH}", response_model=DeviceDetailOut)
async def get_device(uuid: str, db: AsyncSession = Depends(get_db)) -> DeviceDetailOut:
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
async def delete_device(uuid: str, db: AsyncSession = Depends(get_db)) -> DeleteOut:
    return await service.delete_device(db, _uuid(uuid))
