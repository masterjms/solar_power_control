"""단말 설정 API — docs/05 "단말 설정 API (S-23)"."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_rules
from app.core.auth import Principal, current_user
from app.db import get_db
from app.modules.deps import get_publisher, get_settings_sync
from app.modules.settings import service
from app.modules.settings.schemas import (
    HistoryOut,
    SchedulePreviewOut,
    SentOut,
    SettingsOut,
    SettingsPut,
    WriteOut,
)
from app.mqtt.publisher import MqttPublisher
from app.mqtt.settings_sync import SettingsSync

router = APIRouter(tags=["settings"])


def _uuid(uuid: str) -> str:
    return uuid.strip().upper()


@router.get("/api/settings/schema")
async def get_schema() -> dict[str, Any]:
    """ui_items.json 그대로(groups/items/rules/calc/year_table). 화면은 이것으로 폼을 그린다."""
    return settings_rules.schema()


@router.get("/api/schedule/preview", response_model=SchedulePreviewOut)
async def schedule_preview(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    on: int = Query(default=0, ge=-180, le=180),
    off: int = Query(default=0, ge=-180, le=180),
) -> SchedulePreviewOut:
    return service.schedule_preview(lat, lon, on, off)


@router.get("/api/devices/{uuid}/settings", response_model=SettingsOut)
async def get_settings(uuid: str, db: AsyncSession = Depends(get_db)) -> SettingsOut:
    return await service.get_settings(db, _uuid(uuid))


@router.post("/api/devices/{uuid}/settings/read", response_model=SentOut,
             status_code=status.HTTP_202_ACCEPTED)
async def read(
    uuid: str,
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
    publisher: MqttPublisher = Depends(get_publisher),
    ssync: SettingsSync = Depends(get_settings_sync),
) -> SentOut:
    return await service.read(db, _uuid(uuid), me, publisher, ssync)


@router.put("/api/devices/{uuid}/settings", response_model=WriteOut,
            status_code=status.HTTP_202_ACCEPTED)
async def write(
    uuid: str,
    body: SettingsPut,
    force: bool = Query(default=False, description="한 번도 읽지 않은 단말에도 쓴다"),
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
    publisher: MqttPublisher = Depends(get_publisher),
    ssync: SettingsSync = Depends(get_settings_sync),
) -> WriteOut:
    return await service.write(db, _uuid(uuid), body, force, me, publisher, ssync)


@router.post("/api/devices/{uuid}/settings/accept", response_model=SettingsOut)
async def accept(
    uuid: str, db: AsyncSession = Depends(get_db), me: Principal = Depends(current_user),
) -> SettingsOut:
    return await service.accept(db, _uuid(uuid), me)


@router.post("/api/devices/{uuid}/settings/revert", response_model=WriteOut,
             status_code=status.HTTP_202_ACCEPTED)
async def revert(
    uuid: str,
    db: AsyncSession = Depends(get_db),
    me: Principal = Depends(current_user),
    publisher: MqttPublisher = Depends(get_publisher),
    ssync: SettingsSync = Depends(get_settings_sync),
) -> WriteOut:
    return await service.revert(db, _uuid(uuid), me, publisher, ssync)


@router.get("/api/devices/{uuid}/settings/history", response_model=list[HistoryOut])
async def history(
    uuid: str, limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[HistoryOut]:
    return await service.history(db, _uuid(uuid), limit)
