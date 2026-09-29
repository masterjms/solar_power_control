"""/api/alarms — 알람(조치 필요) 화면(S-24, ADR-009)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.modules.alarm import service
from app.modules.alarm.schemas import AlarmOut, AlarmPage

router = APIRouter(tags=["alarms"])


@router.get("/api/alarms", response_model=AlarmPage)
async def list_alarms(
    status: str = Query(default="open", pattern="^(open|closed)$",
                        description="open = 열린 알람, closed = 이력"),
    tab: str | None = Query(default=None, pattern="^(fault|comm|pending|config|local)$"),
    kind: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=64, description="시설명·UUID·주소 부분 일치"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> AlarmPage:
    return await service.list_alarms(db, status=status, tab=tab, kind=kind, q=q, page=page,
                                     size=size)


@router.get("/api/devices/{uuid}/alarms", response_model=list[AlarmOut])
async def device_alarms(
    uuid: str, limit: int = Query(default=30, ge=1, le=500), db: AsyncSession = Depends(get_db),
) -> list[AlarmOut]:
    """그 단말의 열린 알람 전부 + 최근 이력 limit 건."""
    return await service.device_alarms(db, uuid.strip().upper(), limit)
